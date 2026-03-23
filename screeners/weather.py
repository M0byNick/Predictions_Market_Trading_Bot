"""
Weather/Climate Screener

Screens Kalshi weather contracts (daily high temps, monthly rainfall/snowfall)
against free public forecast data from the National Weather Service (NWS).

Why this works: Most Kalshi weather market participants either guess based on
"feels right" or use basic seasonal averages. The NWS publishes detailed
probabilistic forecasts (MOS — Model Output Statistics) that are already quite
good. Simply comparing the NWS point forecast or probabilistic spread against
the Kalshi market price often reveals 10-20% mispricings, especially 3-7 days
out when the models are accurate but the market hasn't updated.

Data sources (all free, no API key):
  - NWS API: https://api.weather.gov — point forecasts, hourly forecasts
  - NWS Daily Climate Reports — settlement source for Kalshi weather contracts
  - GFS/ECMWF model data — available via various free APIs for deeper analysis

Settlement:
  Kalshi weather contracts settle against NWS Daily Climate Report / NOWData.
  This means the NWS's own data is the ground truth — giving us a huge
  advantage when we use NWS forecasts as our probability model.
"""
from __future__ import annotations
import requests
from datetime import datetime, timezone, timedelta
from kalshi_client import KalshiClient
import config
from log import logger
from snapshots import log_snapshot


class WeatherScreener:
    """Screens weather contracts using NWS forecast data."""

    # NWS grid coordinates for target cities (lat, lon)
    # These are the NWS forecast grid points for each city's main weather station
    CITY_COORDS = {
        "NYC": {"lat": 40.7128, "lon": -74.0060, "station": "KNYC",
                "nws_office": "OKX", "gridX": 33, "gridY": 37},
        "CHI": {"lat": 41.8781, "lon": -87.6298, "station": "KORD",
                "nws_office": "LOT", "gridX": 65, "gridY": 76},
        "MIA": {"lat": 25.7617, "lon": -80.1918, "station": "KMIA",
                "nws_office": "MFL", "gridX": 75, "gridY": 56},
        "AUS": {"lat": 30.2672, "lon": -97.7431, "station": "KAUS",
                "nws_office": "EWX", "gridX": 54, "gridY": 98},
    }

    # Kalshi weather series tickers (these may need updating as Kalshi adds markets)
    SERIES_MAP = {
        "NYC": "HIGHNY",
        "CHI": "HIGHCHI",
        "MIA": "HIGHMIA",
        "AUS": "HIGHAUS",
    }

    def __init__(self, client: KalshiClient):
        self.client = client

    def screen(self, cycle_id: str = None) -> list:
        """
        Main screening loop:
          1. For each target city, fetch NWS forecast
          2. Fetch open Kalshi temperature contracts for that city
          3. Compare NWS forecast probabilities to market prices
          4. Flag mispricings above the edge threshold

        Returns a list of trade opportunities.
        """
        opportunities = []
        self._cycle_id = cycle_id

        for city in config.WEATHER_CITIES:
            if city not in self.CITY_COORDS:
                continue

            # Fetch the NWS forecast — this gives us daily high/low temps
            # with a confidence interval we can use for probability estimation
            forecast = self._get_nws_forecast(city)
            if not forecast:
                continue

            # Fetch open Kalshi weather markets for this city
            series = self.SERIES_MAP.get(city)
            if not series:
                continue

            try:
                result = self.client.get_markets(series_ticker=series, limit=50)
                markets = result.get("markets", [])
            except Exception as e:
                logger.error("Error fetching %s weather markets: %s", city, e)
                continue

            for market in markets:
                opp = self._evaluate_market(market, forecast, city)
                if opp:
                    opportunities.append(opp)

        return opportunities

    def _get_nws_forecast(self, city: str) -> list | None:
        """
        Fetch the NWS 7-day forecast for a city.
        Returns a list of forecast periods with high/low temps.

        The NWS API is free and doesn't require an API key, but it does
        require a User-Agent header (per their terms of service).
        """
        coords = self.CITY_COORDS[city]

        try:
            # Step 1: Get the forecast endpoint for this location
            point_resp = requests.get(
                f"{config.NWS_API_BASE}/points/{coords['lat']},{coords['lon']}",
                headers={"User-Agent": "KalshiBot/1.0 (weather-research)"},
                timeout=10,
            )
            point_data = point_resp.json()
            forecast_url = point_data.get("properties", {}).get("forecast")

            if not forecast_url:
                # Fallback: construct URL directly from grid coordinates
                forecast_url = (
                    f"{config.NWS_API_BASE}/gridpoints/"
                    f"{coords['nws_office']}/{coords['gridX']},{coords['gridY']}/forecast"
                )

            # Step 2: Get the actual forecast
            forecast_resp = requests.get(
                forecast_url,
                headers={"User-Agent": "KalshiBot/1.0 (weather-research)"},
                timeout=10,
            )
            forecast_data = forecast_resp.json()
            periods = forecast_data.get("properties", {}).get("periods", [])

            return periods

        except Exception as e:
            logger.warning("NWS forecast fetch failed for %s: %s", city, e)
            return None

    def _get_hourly_forecast(self, city: str) -> list | None:
        """
        Fetch the NWS hourly forecast — gives temperature at each hour,
        which lets us build a more precise probability distribution for
        the daily high.
        """
        coords = self.CITY_COORDS[city]

        try:
            resp = requests.get(
                f"{config.NWS_API_BASE}/gridpoints/"
                f"{coords['nws_office']}/{coords['gridX']},{coords['gridY']}/forecast/hourly",
                headers={"User-Agent": "KalshiBot/1.0 (weather-research)"},
                timeout=10,
            )
            data = resp.json()
            return data.get("properties", {}).get("periods", [])
        except Exception:
            return None

    def _evaluate_market(self, market: dict, forecast: list, city: str) -> dict | None:
        """
        Compare a Kalshi temperature contract against NWS forecast data.

        The key insight: NWS forecasts are generally accurate to within ±3°F
        for 1-3 day forecasts, and ±5°F for 4-7 day forecasts.

        For a contract like "Will NYC daily high be above 65°F on March 25?":
          - If NWS forecasts 72°F high for that day, the probability of >65°F
            is very high (maybe 90%+) given typical forecast error.
          - If the Kalshi market prices it at 70%, that's a 20%+ edge.

        We model forecast uncertainty as a normal distribution centered on the
        NWS point forecast with std dev based on forecast lead time.
        """
        import re
        from scipy.stats import norm

        # Parse the threshold temperature from the contract
        threshold = self._parse_threshold(market)
        if threshold is None:
            return None

        # Parse the target date from the contract
        target_date = self._parse_target_date(market)
        if target_date is None:
            return None

        # Find the matching forecast period
        nws_forecast_high = self._find_forecast_for_date(forecast, target_date)
        if nws_forecast_high is None:
            return None

        # Calculate days until settlement (affects forecast uncertainty)
        now = datetime.now(timezone.utc)
        days_out = (target_date - now.date()).days
        if days_out < 0 or days_out > 14:
            return None  # Only trade on 0-14 day forecasts

        # NWS forecast error standard deviation by lead time (in °F)
        # These are approximate; you should calibrate from historical data.
        # Source: NWS forecast verification studies
        forecast_error_std = {
            0: 2.0,   # Same day — very accurate
            1: 2.5,   # Tomorrow
            2: 3.0,
            3: 3.5,
            4: 4.0,
            5: 4.5,
            6: 5.0,
            7: 5.5,   # A week out — still useful but wider
        }
        std = forecast_error_std.get(min(days_out, 7), 6.0)

        # P(actual_high > threshold) = P(Z > (threshold - forecast) / std)
        # Using normal CDF: 1 - Φ((threshold - forecast) / std)
        z_score = (threshold - nws_forecast_high) / std
        model_prob = 1 - norm.cdf(z_score)

        # Get market price
        market_prob = (market.get("last_price") or market.get("yes_ask") or 50) / 100.0
        if market_prob <= 0 or market_prob >= 1:
            return None

        # Determine edge and side
        edge_yes = model_prob - market_prob
        edge_no = market_prob - model_prob

        snap = {
            "screener": "weather",
            "ticker": market.get("ticker", ""),
            "city": city,
            "forecast_high": nws_forecast_high,
            "threshold": threshold,
            "days_out": days_out,
            "forecast_std": std,
            "model_prob": round(model_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge_yes": round(edge_yes, 4),
            "edge_no": round(edge_no, 4),
        }

        if edge_yes >= config.MIN_EDGE_THRESHOLD:
            side = "yes"
            edge = edge_yes
            your_prob = model_prob
        elif edge_no >= config.MIN_EDGE_THRESHOLD:
            side = "no"
            edge = edge_no
            your_prob = 1 - model_prob
        else:
            snap.update(decision="skip", reason="edge below threshold")
            log_snapshot(snap, cycle_id=getattr(self, "_cycle_id", None))
            return None

        snap.update(decision="trade", side=side, edge=round(edge, 4))
        log_snapshot(snap, cycle_id=getattr(self, "_cycle_id", None))

        return {
            "ticker": market.get("ticker", ""),
            "title": market.get("title", ""),
            "category": "weather",
            "city": city,
            "side": side,
            "your_prob": round(your_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge": round(edge, 4),
            "threshold_temp": threshold,
            "nws_forecast_high": nws_forecast_high,
            "days_out": days_out,
            "forecast_std": std,
            "rationale": (
                f"NWS forecasts {city} high of {nws_forecast_high}°F "
                f"({days_out}d out, σ={std}°F). "
                f"P(above {threshold}°F) = {model_prob:.1%} "
                f"vs market {market_prob:.1%}"
            ),
        }

    def _parse_threshold(self, market: dict) -> float | None:
        """Extract the temperature threshold from the market title."""
        import re
        title = market.get("title", "") + " " + market.get("subtitle", "")
        # Look for patterns like "above 65°F", "65°", "65 degrees"
        matches = re.findall(r'(\d+)\s*°?\s*[Ff]?', title)
        if matches:
            try:
                return float(matches[0])
            except ValueError:
                return None
        return None

    def _parse_target_date(self, market: dict) -> "datetime.date | None":
        """Extract the target date for settlement from the market metadata."""
        close_time_str = market.get("close_time", "") or market.get("expiration_time", "")
        if close_time_str:
            try:
                dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                return dt.date()
            except Exception:
                pass
        return None

    def _find_forecast_for_date(self, forecast_periods: list, target_date) -> float | None:
        """
        Find the forecasted high temperature for a specific date in the
        NWS forecast periods.
        """
        for period in forecast_periods:
            if not period.get("isDaytime", True):
                continue  # Skip nighttime periods — we want daytime highs

            start_time = period.get("startTime", "")
            try:
                period_date = datetime.fromisoformat(start_time.replace("Z", "+00:00")).date()
                if period_date == target_date:
                    temp = period.get("temperature")
                    if temp is not None:
                        return float(temp)
            except Exception:
                continue

        return None
