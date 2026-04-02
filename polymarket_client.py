"""
Polymarket Gamma API client for cross-market edge validation.

Read-only: fetches market prices from Polymarket to compare against our
model probabilities and Kalshi market prices. No trading, no auth needed.

API: https://gamma-api.polymarket.com (free, public)
CLOB: https://clob.polymarket.com (free, public for market data)

Limitations discovered during integration:
- Gamma API has no text search on /markets endpoint (returns default sort)
- CLOB API paginates by creation date — need to scan 750K+ offsets to reach current markets
- Bitcoin price bracket markets exist but are mostly intraday (5-min intervals)
- Fed/CPI/econ markets don't exist in a form matching Kalshi's structure
- Best matches: "Bitcoin above $X on [date] at 5PM ET?" → matches Kalshi KXBTC expiry

Strategy: Cache a batch of current BTC/ETH price level markets from CLOB API,
match by asset + strike + expiry date. Refresh cache every cycle (30 min).
"""
from __future__ import annotations
import requests
import time
import base64
from datetime import datetime, timezone, timedelta
from log import logger
import config


class PolymarketClient:
    """Read-only Polymarket price fetcher for edge validation."""

    CLOB_BASE = "https://clob.polymarket.com"
    GAMMA_BASE = config.POLYMARKET_API_BASE

    # Map our asset tickers to Polymarket question keywords
    ASSET_KEYWORDS = {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth"],
        "SOL": ["solana", "sol"],
    }

    def __init__(self):
        self._cache = {}           # {asset: [{question, price, end_date, strike, strike_type}]}
        self._cache_time = None
        self._cache_ttl = config.POLYMARKET_CACHE_TTL_MINUTES * 60

    def get_price(self, category: str, asset: str, strike: float,
                  expiry_date: str | None, strike_type: str = "greater") -> float | None:
        """
        Find a matching Polymarket market and return its YES probability.

        Args:
            category: "crypto", "weather", or "economics"
            asset: "BTC", "ETH", "SOL", "CPI", "FED_RATE"
            strike: Strike price or threshold
            expiry_date: ISO date string (e.g., "2026-04-03")
            strike_type: "greater", "less", or "between"

        Returns:
            float (0-1 probability) or None if no match found.
        """
        if not config.ENABLE_POLYMARKET_VALIDATION:
            return None

        # Only crypto has matching markets on Polymarket
        if category != "crypto":
            return None

        if asset not in self.ASSET_KEYWORDS:
            return None

        # Refresh cache if stale
        self._ensure_cache(asset)

        # Find best matching market
        return self._match_market(asset, strike, expiry_date, strike_type)

    def _ensure_cache(self, asset: str) -> None:
        """Refresh the market cache if stale or empty."""
        now = time.time()
        if (self._cache_time and
                now - self._cache_time < self._cache_ttl and
                asset in self._cache):
            return

        try:
            markets = self._fetch_current_crypto_markets(asset)
            self._cache[asset] = markets
            self._cache_time = now
            if markets:
                logger.info("Polymarket cache: %d %s markets loaded", len(markets), asset)
        except Exception as e:
            logger.warning("Polymarket cache refresh failed for %s: %s", asset, e)

    def _fetch_current_crypto_markets(self, asset: str) -> list:
        """
        Fetch current crypto price level markets from the CLOB API.

        The CLOB API paginates by creation date. Daily crypto price markets
        are created fresh each day, so we start from a high offset and scan
        backward/forward to find markets with today's or tomorrow's end dates.
        """
        keywords = self.ASSET_KEYWORDS.get(asset, [])
        if not keywords:
            return []

        today = datetime.now(timezone.utc).date()
        tomorrow = today + timedelta(days=1)
        target_dates = {today.isoformat(), tomorrow.isoformat()}

        matched = []

        # Start from a high offset — current daily markets are created recently
        # We scan 3 pages (3000 markets) from a high starting point
        # The offset needs periodic adjustment as more markets are created
        start_offset = 750000  # Approximate offset for March/April 2026 markets

        for page in range(3):
            offset = start_offset + page * 1000
            cursor = base64.b64encode(str(offset).encode()).decode()

            try:
                resp = requests.get(
                    f"{self.CLOB_BASE}/markets",
                    params={"next_cursor": cursor},
                    timeout=15,
                )
                data = resp.json()
                markets = data.get("data", [])
            except Exception as e:
                logger.warning("Polymarket CLOB fetch failed (offset %d): %s", offset, e)
                break

            if not markets:
                break

            for m in markets:
                question = (m.get("question") or "").lower()
                end_date = (m.get("end_date_iso") or "")[:10]

                # Must match asset keyword
                if not any(kw in question for kw in keywords):
                    continue

                # Must be a price level market (not up/down intraday)
                if "up or down" in question:
                    continue

                # Must be for today or tomorrow
                if end_date not in target_dates:
                    continue

                # Must still be open
                if m.get("closed"):
                    continue

                # Extract price from tokens
                tokens = m.get("tokens", [])
                yes_price = None
                for t in tokens:
                    if t.get("outcome") == "Yes":
                        yes_price = t.get("price")
                        break

                if yes_price is None:
                    continue

                # Parse strike from question
                parsed = self._parse_question(question)
                if parsed is None:
                    continue

                matched.append({
                    "question": m.get("question", ""),
                    "price": float(yes_price),
                    "end_date": end_date,
                    "strike": parsed["strike"],
                    "strike_type": parsed["strike_type"],
                    "condition_id": m.get("condition_id", ""),
                })

        return matched

    def _parse_question(self, question: str) -> dict | None:
        """
        Parse a Polymarket question to extract strike and type.

        Examples:
            "Bitcoin above 69,800 on April 1, 5PM ET?" → {strike: 69800, strike_type: "greater"}
            "Will the price of Bitcoin be between $104K and $105K at 5 PM ET today?"
                → {strike: 104000, strike_type: "between"}
            "Will the price of Bitcoin be less than $93000 on Feb 28?"
                → {strike: 93000, strike_type: "less"}
        """
        import re

        question = question.lower()

        # Determine strike type
        if "between" in question:
            strike_type = "between"
        elif "less than" in question or "below" in question:
            strike_type = "less"
        elif "above" in question or "greater than" in question:
            strike_type = "greater"
        else:
            return None

        # Extract dollar amount — handles $100,000 / $100K / 69,800 / 93000
        matches = re.findall(r'\$?([\d,]+(?:\.\d+)?)\s*([kK])?', question)
        if not matches:
            return None

        # Filter out small numbers that are likely dates/times (e.g., "5pm", "2026")
        valid_matches = []
        for val_str, k_suffix in matches:
            clean = val_str.replace(",", "")
            try:
                val = float(clean)
            except ValueError:
                continue
            if k_suffix:
                val *= 1000
            # Skip numbers that look like years, times, or very small
            if val < 100 or (1900 < val < 2100):
                continue
            valid_matches.append(val)

        if not valid_matches:
            return None

        strike = valid_matches[0]

        return {"strike": strike, "strike_type": strike_type}

    def _match_market(self, asset: str, strike: float, expiry_date: str | None,
                      strike_type: str) -> float | None:
        """
        Find the best matching Polymarket market for a Kalshi contract.

        Matching criteria:
        1. Same strike type (greater/less/between)
        2. Strike price within 5% of Kalshi strike
        3. Same or adjacent expiry date
        """
        markets = self._cache.get(asset, [])
        if not markets:
            return None

        best_match = None
        best_distance = float("inf")

        for m in markets:
            # Must match strike type
            if m["strike_type"] != strike_type:
                continue

            # Check expiry date (same day or adjacent)
            if expiry_date and m["end_date"]:
                date_match = m["end_date"] == expiry_date[:10]
                if not date_match:
                    continue

            # Strike distance (relative)
            if strike > 0:
                distance = abs(m["strike"] - strike) / strike
            else:
                continue

            # Within 5% of strike
            if distance > 0.05:
                continue

            if distance < best_distance:
                best_distance = distance
                best_match = m

        if best_match:
            return best_match["price"]

        return None
