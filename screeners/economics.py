"""
Economics Data Release Screener

Screens Kalshi contracts on economic data releases (CPI, Fed rate decisions,
nonfarm payrolls, GDP, unemployment) by comparing market prices against
consensus estimates and your own research.

Edge source: The market is already quite good at pricing these (per Fed
research, Kalshi outperforms Bloomberg consensus on headline CPI). So
the edge here is smaller than crypto or weather, and comes from:
  1. Timing: getting positioned before consensus shifts (e.g., a strong
     ADP report before NFP, or PPI data that previews CPI)
  2. Reading primary data sources: BLS methodological notes, Fed minutes
     tone, regional Fed surveys, leading indicators
  3. Tail risk: markets tend to underprice extreme outcomes on economic data

Data sources:
  - FRED (Federal Reserve Economic Data) — free with API key
  - BLS (Bureau of Labor Statistics) — release schedules and data
  - Federal Reserve — FOMC statements, dot plots, minutes
"""
from __future__ import annotations
import requests
from datetime import datetime, timezone, timedelta
from kalshi_client import KalshiClient
import config
from log import logger
from screeners.utils import get_market_prob
from snapshots import log_snapshot


class EconomicsScreener:
    """Screens economic data release contracts on Kalshi."""

    # Kalshi series tickers for economic events
    SERIES_MAP = {
        "CPI": "KXCPI",
        "CPI_YOY": "KXCPIYOY",
        "FED_RATE": "KXFED",
    }

    # FRED series IDs for each economic indicator
    FRED_SERIES = {
        "CPI": "CPIAUCSL",               # CPI All Urban Consumers
        "CPI_YOY": "CPIAUCNS",           # CPI YoY (not seasonally adjusted)
        "FED_RATE": "DFEDTARU",           # Fed Funds upper target
        "UNEMPLOYMENT": "UNRATE",          # Unemployment rate
        "NONFARM": "PAYEMS",              # Total nonfarm payrolls
        "GDP": "GDP",                      # Gross domestic product
    }

    def __init__(self, client: KalshiClient):
        self.client = client

    def screen(self, cycle_id: str = None) -> list:
        """
        Main screening loop:
          1. Fetch recent economic data from FRED to understand current trends
          2. Fetch open Kalshi economic contracts
          3. Compare market pricing to trend-based estimates
          4. Flag potential mispricings, especially on tails

        Returns a list of trade opportunities.
        """
        opportunities = []
        self._cycle_id = cycle_id

        for event_type in config.ECON_EVENTS:
            series = self.SERIES_MAP.get(event_type)
            if not series:
                continue

            # Fetch historical data from FRED for context
            historical = self._get_fred_data(event_type)

            # Fetch open Kalshi markets
            try:
                result = self.client.get_markets(series_ticker=series, limit=50)
                markets = result.get("markets", [])
            except Exception as e:
                logger.error("Error fetching %s markets: %s", event_type, e)
                continue

            for market in markets:
                opp = self._evaluate_market(market, historical, event_type)
                if opp:
                    opportunities.append(opp)

        return opportunities

    def _get_fred_data(self, event_type: str, observations: int = 24) -> dict | None:
        """
        Fetch recent data from FRED for a given indicator.
        Returns the last N observations so we can see the trend.
        
        FRED API is free — get a key at https://fred.stlouisfed.org/docs/api/api_key.html
        """
        if not config.FRED_API_KEY:
            return None

        series_id = self.FRED_SERIES.get(event_type)
        if not series_id:
            return None

        try:
            resp = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": config.FRED_API_KEY,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": observations,
                },
                timeout=10,
            )
            data = resp.json()
            obs = data.get("observations", [])

            if not obs:
                return None

            # Parse values into floats
            values = []
            for o in obs:
                try:
                    values.append({
                        "date": o["date"],
                        "value": float(o["value"]),
                    })
                except (ValueError, KeyError):
                    continue

            if not values:
                return None

            # Compute trend statistics
            recent_values = [v["value"] for v in values[:6]]  # Last 6 months
            n = len(recent_values)
            mean_6m = sum(recent_values) / n

            # Standard deviation of recent values
            if n >= 3:
                variance = sum((v - mean_6m) ** 2 for v in recent_values) / (n - 1)
                std_6m = variance ** 0.5
            else:
                std_6m = None

            # Month-over-month changes (for CPI-type contracts)
            changes = []
            for i in range(len(values) - 1):
                changes.append(values[i]["value"] - values[i + 1]["value"])
            changes_6m = changes[:6] if len(changes) >= 6 else changes

            return {
                "series_id": series_id,
                "latest": values[0],
                "previous": values[1] if len(values) > 1 else None,
                "mean_6m": mean_6m,
                "std_6m": std_6m,
                "min_6m": min(recent_values),
                "max_6m": max(recent_values),
                "changes": changes_6m,
                "trend": self._compute_trend(recent_values),
                "values": values[:12],
            }

        except Exception as e:
            logger.warning("FRED fetch failed for %s: %s", event_type, e)
            return None

    def _compute_trend(self, values: list) -> str:
        """
        Simple trend classification based on last 6 data points.
        Returns 'rising', 'falling', or 'stable'.
        """
        if len(values) < 3:
            return "insufficient_data"

        # Compare first half average to second half average
        mid = len(values) // 2
        recent_avg = sum(values[:mid]) / mid
        older_avg = sum(values[mid:]) / (len(values) - mid)

        pct_change = (recent_avg - older_avg) / older_avg if older_avg else 0

        if pct_change > 0.02:
            return "rising"
        elif pct_change < -0.02:
            return "falling"
        return "stable"

    def _model_probability(self, historical: dict, threshold: float,
                           event_type: str, months_forward: int = 1) -> float | None:
        """
        Estimate P(next_value > threshold) using a normal distribution
        fitted to recent FRED data.

        months_forward: how many months until the contract's data release.
        Uncertainty grows with sqrt(months_forward) to account for the
        fact that we can't predict the economy 10 months out as well as 1.

        Returns probability (0-1) or None if insufficient data.
        """
        from scipy.stats import norm
        import math

        # Uncertainty scaling factor: σ grows with sqrt of time
        # 1 month → 1.0x, 4 months → 2.0x, 9 months → 3.0x
        uncertainty_scale = max(math.sqrt(months_forward), 1.0)

        if event_type in ("CPI", "NONFARM"):
            # Model the change distribution
            changes = historical.get("changes", [])
            if len(changes) < 3:
                return None
            mu = sum(changes) / len(changes)
            variance = sum((c - mu) ** 2 for c in changes) / (len(changes) - 1)
            sigma = max(variance ** 0.5, 0.001) * uncertainty_scale
            return float(1 - norm.cdf(threshold, loc=mu, scale=sigma))

        elif event_type == "CPI_YOY":
            std = historical.get("std_6m")
            if std is None or std < 0.001:
                return None
            mu = historical["mean_6m"]
            return float(1 - norm.cdf(threshold, loc=mu, scale=std * uncertainty_scale))

        elif event_type == "FED_RATE":
            # Fed rate: each FOMC meeting is ~6 weeks apart, rate can
            # move 0-50bp per meeting. Historical rate cycles show the
            # Fed can move 200-300bp in a year during cutting/hiking cycles.
            # σ scales aggressively with time: 0.50 * sqrt(months)
            # 1 month → 0.50, 6 months → 1.22, 12 months → 1.73
            latest = historical["latest"]["value"]
            sigma = 0.50 * math.sqrt(max(months_forward, 1))
            return float(1 - norm.cdf(threshold, loc=latest, scale=sigma))

        else:
            # GDP, UNEMPLOYMENT: model level distribution
            std = historical.get("std_6m")
            if std is None or std < 0.001:
                return None
            mu = historical["mean_6m"]
            return float(1 - norm.cdf(threshold, loc=mu, scale=std * uncertainty_scale))

    def _heuristic_flags(self, historical: dict, threshold: float | None,
                         market_prob: float) -> list:
        """
        Original heuristic flags — kept as supporting evidence in rationale.
        No longer used for edge estimation.
        """
        flags = []
        if historical and threshold is not None:
            latest_value = historical["latest"]["value"]
            trend = historical["trend"]

            if trend == "rising" and market_prob < 0.50:
                flags.append("trend_rising")
            elif trend == "falling" and market_prob > 0.50:
                flags.append("trend_falling")

            if market_prob < 0.10:
                if historical["min_6m"] <= threshold <= historical["max_6m"]:
                    flags.append("cheap_tail_in_range")

            if latest_value:
                distance_pct = abs(latest_value - threshold) / latest_value if latest_value else 0
                if distance_pct < 0.02:
                    flags.append("near_threshold")

        return flags

    def _evaluate_market(self, market: dict, historical: dict | None,
                         event_type: str) -> dict | None:
        """
        Evaluate a Kalshi economics contract using a quantitative
        normal distribution model fitted to recent FRED data.

        Phase 5 upgrade: replaces heuristic edge estimates with
        model-driven probabilities. Heuristic flags are kept as
        supporting evidence in the rationale.
        """
        # Get market price
        market_prob = get_market_prob(market)
        if market_prob <= 0 or market_prob >= 1:
            return None

        # Parse threshold from market title
        threshold = self._parse_threshold(market, event_type)

        # Compute months until data release from close_time
        months_forward = 1
        close_time_str = market.get("close_time", "") or market.get("expiration_time", "")
        if close_time_str:
            try:
                from datetime import datetime, timezone
                close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                months_forward = max(int((close_dt - now).days / 30), 1)
            except Exception:
                pass

        # Try quantitative model
        model_prob = None
        model_std = None
        if historical and threshold is not None:
            model_prob = self._model_probability(
                historical, threshold, event_type, months_forward=months_forward
            )
            model_std = historical.get("std_6m")

        # Heuristic flags for rationale (not used for edge)
        flags = self._heuristic_flags(historical, threshold, market_prob) if historical else []

        snap = {
            "screener": "economics",
            "ticker": market.get("ticker", ""),
            "event_type": event_type,
            "months_forward": months_forward,
            "market_prob": round(market_prob, 4),
            "model_prob": round(model_prob, 4) if model_prob is not None else None,
            "model_std": round(model_std, 4) if model_std is not None else None,
            "flags": flags,
            "trend": historical.get("trend", "unknown") if historical else "no_data",
            "latest_value": historical["latest"]["value"] if historical else None,
        }

        if model_prob is None:
            snap.update(decision="skip", reason="no model probability")
            log_snapshot(snap, cycle_id=getattr(self, "_cycle_id", None))
            return None

        # Determine side and edge from quantitative model
        edge_yes = model_prob - market_prob
        edge_no = market_prob - model_prob

        if edge_yes >= config.MIN_EDGE_THRESHOLD:
            side = "yes"
            edge = edge_yes
            your_prob = model_prob
        elif edge_no >= config.MIN_EDGE_THRESHOLD:
            side = "no"
            edge = edge_no
            your_prob = 1 - model_prob
        else:
            snap.update(decision="skip", reason="edge below threshold",
                        edge_yes=round(edge_yes, 4), edge_no=round(edge_no, 4))
            log_snapshot(snap, cycle_id=getattr(self, "_cycle_id", None))
            return None

        snap.update(decision="trade", side=side, edge=round(edge, 4))
        log_snapshot(snap, cycle_id=getattr(self, "_cycle_id", None))

        flag_str = f" Flags: {', '.join(flags)}." if flags else ""
        return {
            "ticker": market.get("ticker", ""),
            "title": market.get("title", ""),
            "category": "economics",
            "event_type": event_type,
            "side": side,
            "your_prob": round(your_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge": round(edge, 4),
            "flags": flags,
            "trend": historical.get("trend", "unknown") if historical else "no_data",
            "latest_value": historical["latest"]["value"] if historical else None,
            "model_std": round(model_std, 4) if model_std is not None else None,
            "rationale": (
                f"Quantitative model: P(>{threshold}) = {model_prob:.1%} "
                f"vs market {market_prob:.1%}. "
                f"μ={historical['mean_6m']:.2f}, σ={model_std:.3f}. "
                f"Trend: {historical.get('trend', 'N/A')}."
                + flag_str
            ) if historical and model_std else (
                f"Model: P(>{threshold}) = {model_prob:.1%} "
                f"vs market {market_prob:.1%}."
                + flag_str
            ),
        }

    def _parse_threshold(self, market: dict, event_type: str) -> float | None:
        """Extract the numeric threshold from the contract title."""
        import re
        title = market.get("title", "") + " " + market.get("subtitle", "")

        # Look for percentage values like "3.5%" or "3.5 percent"
        pct_matches = re.findall(r'(\d+\.?\d*)\s*%', title)
        if pct_matches:
            try:
                return float(pct_matches[0])
            except ValueError:
                pass

        # Look for basis points or rate values like "5.25" or "525 bps"
        num_matches = re.findall(r'(\d+\.?\d+)', title)
        if num_matches:
            try:
                return float(num_matches[0])
            except ValueError:
                pass

        return None

    def get_upcoming_releases(self) -> list:
        """
        Get a schedule of upcoming economic data releases.
        Useful for knowing when to be extra attentive to the screener.

        This is a simplified version — in production you'd pull from
        the BLS release calendar or a financial data provider.
        """
        # Typical monthly schedule (approximate — verify each month)
        typical_schedule = [
            {"event": "Nonfarm Payrolls", "typical_day": "First Friday",
             "time": "8:30 AM ET"},
            {"event": "CPI", "typical_day": "~12th of month",
             "time": "8:30 AM ET"},
            {"event": "FOMC Decision", "typical_day": "~6 weeks apart",
             "time": "2:00 PM ET"},
            {"event": "GDP (advance)", "typical_day": "~28th of month",
             "time": "8:30 AM ET"},
        ]
        return typical_schedule
