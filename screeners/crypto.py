"""
Crypto Events Screener

Screens Kalshi crypto price bracket contracts (BTC, ETH, SOL) for mispricings.
Uses a simple volatility-based probability model as a baseline, which you should
enhance with your own crypto domain knowledge over time.

The core idea: Kalshi's weekly/monthly price bracket contracts are priced by
retail participants who often anchor on recent price action and neglect
volatility regimes, on-chain signals, and macro catalysts. Your edge comes
from doing that analysis before they do.

Contract structure on Kalshi:
  - "Will BTC be above $100,000 on March 28?" → YES/NO binary
  - Series tickers like KXBTC (BTC), KXETH (ETH), KXSOL (SOL)
  - Settlement: CF Benchmarks Real-Time Index, 60-second average at expiry
"""
from __future__ import annotations
import json
import math
import os
from datetime import datetime, timezone, timedelta
import requests
from kalshi_client import KalshiClient
import config
from log import logger

VOL_CACHE_FILE = os.path.join("data", "vol_cache.json")
VOL_CACHE_MAX_AGE_SECONDS = 4 * 3600  # 4 hours


class CryptoScreener:
    """Screens crypto price bracket contracts for mispricings."""

    # Series tickers on Kalshi for each crypto asset
    SERIES_MAP = {
        "BTC": "KXBTC",
        "ETH": "KXETH",
        "SOL": "KXSOL",
    }

    def __init__(self, client: KalshiClient):
        self.client = client

    def screen(self) -> list:
        """
        Main screening loop. For each configured crypto ticker:
          1. Fetch current spot price (via CoinGecko, free, no key)
          2. Fetch open Kalshi contracts in that series
          3. Estimate fair probability for each strike using a log-normal model
          4. Flag contracts where market price deviates from our estimate

        Returns a list of trade opportunities with sizing info.
        """
        opportunities = []

        for ticker in config.CRYPTO_TICKERS:
            series = self.SERIES_MAP.get(ticker)
            if not series:
                continue

            # Get current spot price from CoinGecko (free API, no key needed)
            spot = self._get_spot_price(ticker)
            if spot is None:
                continue

            # Get historical daily volatility (30-day realized vol)
            vol = self._get_realized_vol(ticker)
            if vol is None:
                logger.warning("Skipping %s: no vol data available (API down, cache stale)", ticker)
                continue

            # Fetch open markets in this series
            try:
                result = self.client.get_markets(series_ticker=series, limit=100)
                markets = result.get("markets", [])
            except Exception as e:
                logger.error("Error fetching %s markets: %s", series, e)
                continue

            # Filter for weekly and monthly timeframes (skip 15-min day-trading contracts)
            markets = [m for m in markets if self._is_target_timeframe(m)]

            for market in markets:
                opp = self._evaluate_market(market, spot, vol, ticker)
                if opp:
                    opportunities.append(opp)

        return opportunities

    def _get_spot_price(self, ticker: str) -> float | None:
        """Fetch current spot price from CoinGecko's free API."""
        coin_ids = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
        coin_id = coin_ids.get(ticker)
        if not coin_id:
            return None

        try:
            resp = requests.get(
                f"https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
                timeout=10,
            )
            data = resp.json()
            return data.get(coin_id, {}).get("usd")
        except Exception as e:
            logger.warning("CoinGecko price fetch failed for %s: %s", ticker, e)
            return None

    def _get_realized_vol(self, ticker: str, days: int = 30) -> float | None:
        """
        Fetch 30-day realized volatility from CoinGecko.
        Returns annualized volatility as a decimal (e.g., 0.65 = 65%),
        or None if data is unavailable and cache is stale (>4h).

        On success, caches the result to data/vol_cache.json.
        On failure, falls back to cached value if fresh enough.
        """
        coin_ids = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
        coin_id = coin_ids.get(ticker, "bitcoin")

        try:
            resp = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days},
                timeout=10,
            )
            prices = resp.json().get("prices", [])
            if len(prices) < 10:
                return self._read_vol_cache(ticker)

            closes = [p[1] for p in prices]
            log_returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
            daily_vol = (sum(r**2 for r in log_returns) / len(log_returns)) ** 0.5
            annualized_vol = daily_vol * math.sqrt(365)

            self._write_vol_cache(ticker, annualized_vol)
            return annualized_vol

        except Exception as e:
            logger.warning("CoinGecko vol fetch failed for %s: %s", ticker, e)
            return self._read_vol_cache(ticker)

    def _read_vol_cache(self, ticker: str) -> float | None:
        """Read cached vol for a ticker. Returns None if missing or stale (>4h)."""
        try:
            with open(VOL_CACHE_FILE, "r") as f:
                cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

        entry = cache.get(ticker)
        if entry is None:
            return None

        cached_time = datetime.fromisoformat(entry["timestamp"])
        age = (datetime.now(timezone.utc) - cached_time).total_seconds()
        if age > VOL_CACHE_MAX_AGE_SECONDS:
            logger.warning("Vol cache for %s is stale (%.1fh old), skipping", ticker, age/3600)
            return None

        logger.info("Using cached vol for %s: %.4f (%.0fm old)", ticker, entry['vol'], age/60)
        return entry["vol"]

    def _write_vol_cache(self, ticker: str, vol: float) -> None:
        """Write vol to cache file."""
        try:
            with open(VOL_CACHE_FILE, "r") as f:
                cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}

        cache[ticker] = {
            "vol": round(vol, 6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        os.makedirs(os.path.dirname(VOL_CACHE_FILE), exist_ok=True)
        with open(VOL_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)

    def _is_target_timeframe(self, market: dict) -> bool:
        """
        Filter for weekly and monthly contracts only.
        Skip 15-minute ultra-short contracts (those are day trading).
        """
        # Check close time — if it's more than 1 day out, it's not a 15-min contract
        close_time_str = market.get("close_time", "")
        if not close_time_str:
            return True  # If we can't tell, include it

        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_to_expiry = (close_time - now).total_seconds() / 86400

            # Include contracts expiring in 1-60 days (weekly and monthly)
            return 1 <= days_to_expiry <= 60
        except Exception:
            return True

    def _evaluate_market(self, market: dict, spot: float, vol: float,
                         ticker: str) -> dict | None:
        """
        Compare the market's implied probability to our model's estimate.

        The model: simple log-normal diffusion (Black-Scholes-esque).
        For a "Will BTC be above $X on date Y?" contract, the probability is:
            P(S_T > K) = N(d2)
        where d2 = (ln(S/K) + (mu - 0.5*sigma^2)*T) / (sigma * sqrt(T))

        We assume mu ≈ 0 (no drift — conservative) so it's purely vol-driven.
        """
        # Parse strike price from market title or subtitle
        strike = self._parse_strike(market)
        if strike is None or strike <= 0:
            return None

        # Time to expiry in years
        close_time_str = market.get("close_time", "")
        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            T = max((close_time - now).total_seconds() / (365.25 * 86400), 0.001)
        except Exception:
            return None

        # Log-normal probability: P(S_T > K)
        # d2 = (ln(S/K) - 0.5 * vol^2 * T) / (vol * sqrt(T))
        from scipy.stats import norm
        d2 = (math.log(spot / strike) - 0.5 * vol**2 * T) / (vol * math.sqrt(T))
        model_prob = norm.cdf(d2)

        # Get market price (as probability)
        market_prob = (market.get("last_price") or market.get("yes_ask") or 50) / 100.0
        if market_prob <= 0 or market_prob >= 1:
            return None

        # Determine side and edge
        edge_yes = model_prob - market_prob
        edge_no = (1 - model_prob) - (1 - market_prob)  # Equivalent to market_prob - model_prob

        # Pick the side with positive edge above threshold
        if edge_yes >= config.MIN_EDGE_THRESHOLD:
            side = "yes"
            edge = edge_yes
            your_prob = model_prob
        elif edge_no >= config.MIN_EDGE_THRESHOLD:
            side = "no"
            edge = edge_no
            your_prob = 1 - model_prob
        else:
            return None  # No tradeable edge

        return {
            "ticker": market.get("ticker", ""),
            "title": market.get("title", ""),
            "category": "crypto",
            "asset": ticker,
            "side": side,
            "your_prob": round(your_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge": round(edge, 4),
            "strike": strike,
            "spot": spot,
            "vol": round(vol, 4),
            "days_to_expiry": round(T * 365.25, 1),
            "rationale": (
                f"Log-normal model: {ticker} spot=${spot:,.0f}, "
                f"strike=${strike:,.0f}, vol={vol:.0%}, "
                f"T={T*365.25:.1f}d → P(above)={model_prob:.1%} "
                f"vs market {market_prob:.1%}"
            ),
        }

    def _parse_strike(self, market: dict) -> float | None:
        """
        Extract the strike price from the market title.
        Kalshi titles look like: "Bitcoin above $100,000?" or similar.
        """
        import re
        title = market.get("title", "") + " " + market.get("subtitle", "")
        # Look for dollar amounts like $100,000 or $100000 or $3,500.50
        matches = re.findall(r'\$[\d,]+(?:\.\d+)?', title)
        if matches:
            price_str = matches[0].replace('$', '').replace(',', '')
            try:
                return float(price_str)
            except ValueError:
                return None

        # Also try to extract from ticker (e.g., KXBTC-26MAR28-T100000)
        ticker = market.get("ticker", "")
        t_match = re.search(r'T(\d+)', ticker)
        if t_match:
            return float(t_match.group(1))

        return None
