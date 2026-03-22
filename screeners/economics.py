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


class EconomicsScreener:
    """Screens economic data release contracts on Kalshi."""

    # Kalshi series tickers for economic events (may need updating)
    SERIES_MAP = {
        "CPI": "KXCPI",
        "FED_RATE": "FED",
        "NONFARM": "KXNFP",
        "GDP": "KXGDP",
        "UNEMPLOYMENT": "KXUNEMP",
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

    def screen(self) -> list:
        """
        Main screening loop:
          1. Fetch recent economic data from FRED to understand current trends
          2. Fetch open Kalshi economic contracts
          3. Compare market pricing to trend-based estimates
          4. Flag potential mispricings, especially on tails

        Returns a list of trade opportunities.
        """
        opportunities = []

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
                print(f"Error fetching {event_type} markets: {e}")
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
            return {
                "series_id": series_id,
                "latest": values[0],
                "previous": values[1] if len(values) > 1 else None,
                "mean_6m": sum(recent_values) / len(recent_values),
                "min_6m": min(recent_values),
                "max_6m": max(recent_values),
                "trend": self._compute_trend(recent_values),
                "values": values[:12],
            }

        except Exception as e:
            print(f"FRED fetch failed for {event_type}: {e}")
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

    def _evaluate_market(self, market: dict, historical: dict | None,
                         event_type: str) -> dict | None:
        """
        Evaluate a specific Kalshi economics contract.

        Since we don't have strong predictive models for economic data
        (that's the hard part — institutional quants spend billions on this),
        we focus on two types of edges:

        1. TAIL MISPRICING: Markets systematically underprice extreme outcomes.
           If the contract asks "Will CPI be above 4.0%?" and it's priced at 3%,
           but recent CPI has been trending up and is at 3.5%, the tail risk
           is probably higher than 3%.

        2. TREND DISAGREEMENT: If the trend is clearly rising but the market
           prices a "will it be below X" contract as if it's a coin flip,
           the market may be anchoring on older data.

        This screener is intentionally conservative — it flags opportunities
        for your manual review rather than auto-trading.
        """
        import re

        # Get market price
        market_prob = (market.get("last_price") or market.get("yes_ask") or 50) / 100.0
        if market_prob <= 0 or market_prob >= 1:
            return None

        # We can't build a strong probability model without deep macro research,
        # so this screener flags markets for manual review based on heuristics.
        # The flags are:

        flags = []
        estimated_edge = 0.0
        your_prob = market_prob  # Default: trust the market

        if historical:
            latest_value = historical["latest"]["value"]
            trend = historical["trend"]

            # Parse threshold from market title
            threshold = self._parse_threshold(market, event_type)

            if threshold is not None:
                # Heuristic 1: Is the market ignoring the trend?
                if trend == "rising" and "above" in market.get("title", "").lower():
                    # Rising trend + "above X" contract → market might be too low
                    if market_prob < 0.50:
                        flags.append("trend_bullish_but_market_bearish")
                        # Modest edge estimate — this is heuristic, not model
                        estimated_edge = 0.08

                elif trend == "falling" and "below" in market.get("title", "").lower():
                    if market_prob < 0.50:
                        flags.append("trend_bearish_but_market_bullish")
                        estimated_edge = 0.08

                # Heuristic 2: Tail mispricing — very cheap contracts on plausible outcomes
                if market_prob < 0.10:
                    # Is this outcome actually within recent range?
                    if threshold and historical["min_6m"] <= threshold <= historical["max_6m"]:
                        flags.append("cheap_tail_within_recent_range")
                        estimated_edge = 0.05

                # Heuristic 3: Current value already near threshold
                if threshold:
                    distance_pct = abs(latest_value - threshold) / latest_value if latest_value else 0
                    if distance_pct < 0.02 and (market_prob < 0.35 or market_prob > 0.65):
                        flags.append("close_to_threshold_but_extreme_pricing")
                        estimated_edge = 0.10

        if not flags:
            return None  # Nothing interesting here

        # Determine side based on flags
        side = "yes"
        your_prob = min(market_prob + estimated_edge, 0.95)

        if "bearish" in str(flags) and "below" in market.get("title", "").lower():
            side = "yes"
        elif "bullish" in str(flags) and "above" in market.get("title", "").lower():
            side = "yes"

        edge = your_prob - market_prob if side == "yes" else market_prob - your_prob

        if edge < config.MIN_EDGE_THRESHOLD:
            return None

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
            "manual_review": True,  # Always flag econ trades for manual review
            "trend": historical.get("trend", "unknown") if historical else "no_data",
            "latest_value": historical["latest"]["value"] if historical else None,
            "rationale": (
                f"Econ screener flags: {', '.join(flags)}. "
                f"Trend: {historical.get('trend', 'N/A') if historical else 'N/A'}. "
                f"Latest: {historical['latest']['value'] if historical else 'N/A'}. "
                f"⚠️ Manual review recommended — edge is heuristic, not model-driven."
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
