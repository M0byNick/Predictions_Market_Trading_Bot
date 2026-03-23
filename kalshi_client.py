"""
Kalshi REST API client.
Handles RSA-based authentication, market data retrieval, and order management.
Reference: https://trading-api.readme.io/reference
"""
from __future__ import annotations
import time
import json
import base64
import hashlib
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
import config


class KalshiClient:
    """Wrapper around Kalshi's v2 trading API with RSA signature auth."""

    def __init__(self):
        self.base_url = config.KALSHI_API_BASE
        self.email = config.KALSHI_EMAIL
        self.api_id = config.KALSHI_API_ID
        self.private_key = self._load_private_key()

    def _load_private_key(self):
        """Load the RSA private key from the PEM file configured in config.py."""
        with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign_request(self, method: str, path: str, timestamp_ms: int) -> str:
        """
        Build the Kalshi v2 signature. The signed message is:
            timestamp_ms + method_uppercase + path
        Signed with RSA-PSS using SHA-256.
        """
        message = f"{timestamp_ms}{method.upper()}{path}"
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            utils.Prehashed(hashes.SHA256()) if False else hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        """Generate auth headers for a given request method and path."""
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        sig = self._sign_request(method, path, ts)
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_id or self.email,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }

    def _get(self, path: str, params: dict = None) -> dict:
        """Authenticated GET request."""
        url = f"{self.base_url}{path}"
        headers = self._headers("GET", path)
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        """Authenticated POST request."""
        url = f"{self.base_url}{path}"
        headers = self._headers("POST", path)
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Market Data ──────────────────────────────────────────────────────

    def get_markets(self, series_ticker: str = None, status: str = "open",
                    limit: int = 200, cursor: str = None) -> dict:
        """
        Fetch available markets. Can filter by series_ticker (e.g., 'KXBTC')
        to get all contracts in a series, or fetch broadly.
        """
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._get("/markets", params)

    def get_market(self, ticker: str) -> dict:
        """Get details for a single market/contract by its ticker."""
        return self._get(f"/markets/{ticker}")

    def get_market_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """
        Fetch the order book for a market. Returns bids and asks with
        price levels and quantities. Useful for seeing true liquidity.
        """
        return self._get(f"/markets/{ticker}/orderbook", {"depth": depth})

    def get_series(self, series_ticker: str) -> dict:
        """Get metadata for a series (a group of related contracts)."""
        return self._get(f"/series/{series_ticker}")

    def get_events(self, series_ticker: str = None, status: str = None,
                   limit: int = 100) -> dict:
        """Fetch events (which contain markets). Good for browsing categories."""
        params = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return self._get("/events", params)

    # ── Trading ──────────────────────────────────────────────────────────

    def place_order(self, ticker: str, side: str, size: int,
                    order_type: str = "limit", price: int = None) -> dict:
        """
        Place an order on a market.

        Args:
            ticker: Contract ticker (e.g., 'KXBTC-26MAR21-T100000')
            side: 'yes' or 'no'
            size: Number of contracts (each contract = $1 at settlement)
            order_type: 'limit' or 'market'
            price: Price in cents (1-99) for limit orders. Represents
                   your max willingness to pay per contract.
        """
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": size,
            "type": order_type,
        }
        if order_type == "limit" and price is not None:
            body["yes_price"] = price if side == "yes" else None
            body["no_price"] = price if side == "no" else None
        return self._post("/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by ID."""
        return self._post(f"/portfolio/orders/{order_id}/cancel", {})

    def get_order(self, order_id: str) -> dict:
        """
        Get the status of a specific order.
        Returns order details including status and fill information.
        """
        return self._get(f"/portfolio/orders/{order_id}")

    def get_positions(self) -> dict:
        """Get all current open positions in your portfolio."""
        return self._get("/portfolio/positions")

    def get_balance(self) -> dict:
        """Get current account balance."""
        return self._get("/portfolio/balance")

    # ── Convenience Methods ──────────────────────────────────────────────

    def search_markets(self, query: str, limit: int = 50) -> list:
        """
        Search markets by keyword. Useful for finding specific events
        like 'CPI March 2026' or 'BTC weekly'.
        """
        result = self._get("/markets", {"status": "open", "limit": limit})
        markets = result.get("markets", [])
        query_lower = query.lower()
        return [m for m in markets if query_lower in m.get("title", "").lower()
                or query_lower in m.get("ticker", "").lower()]

    def get_market_with_book(self, ticker: str) -> dict:
        """
        Convenience: fetch market details AND orderbook in one call.
        Returns combined dict with 'market' and 'orderbook' keys.
        """
        market = self.get_market(ticker)
        book = self.get_market_orderbook(ticker)
        return {"market": market, "orderbook": book}

    def get_midpoint(self, ticker: str) -> float | None:
        """
        Calculate the midpoint price from the order book.
        Returns a float between 0 and 1 (probability), or None if
        the book is empty on either side.
        """
        book = self.get_market_orderbook(ticker, depth=1)
        bids = book.get("orderbook", {}).get("yes", [])
        asks = book.get("orderbook", {}).get("no", [])
        if not bids or not asks:
            return None
        # Kalshi prices are in cents (1-99)
        best_bid = bids[0][0] / 100.0
        best_ask = 1 - (asks[0][0] / 100.0)
        return (best_bid + best_ask) / 2.0
