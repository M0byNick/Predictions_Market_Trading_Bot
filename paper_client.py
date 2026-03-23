"""
Paper trading client — drop-in replacement for KalshiClient.

Simulates order placement and fills locally without hitting the Kalshi API.
Real market data is still fetched from Kalshi for screening, but all orders
are simulated with instant fills at the requested price.

Usage:
    # In main.py or anywhere KalshiClient is used:
    if config.PAPER_TRADING:
        from paper_client import PaperClient
        client = PaperClient()
    else:
        from kalshi_client import KalshiClient
        client = KalshiClient()
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from kalshi_client import KalshiClient
import config
from log import logger

PAPER_TRADES_FILE = os.path.join("data", "paper_trades.json")
PAPER_STARTING_BALANCE = 100_000_000  # cents ($1M paper trading)


class PaperClient(KalshiClient):
    """
    Extends KalshiClient — uses real API for market data but simulates trades.

    All get_* methods hit the live Kalshi API so screeners see real prices.
    place_order(), cancel_order(), get_order(), get_positions(), get_balance()
    are simulated locally.
    """

    def __init__(self):
        # Try to init parent for real market data access; if no key, set up
        # minimal state so screeners can still fetch public market data.
        try:
            super().__init__()
            self._has_api = True
        except (FileNotFoundError, Exception) as e:
            logger.warning("No Kalshi API key — paper client will use mock market data: %s", e)
            self.base_url = config.KALSHI_API_BASE
            self.email = config.KALSHI_EMAIL
            self.private_key = None
            self._has_api = False

        self._state = self._load_state()
        logger.info("Paper trading mode | Balance: $%.2f | %d open positions",
                     self._state["balance"] / 100, len(self._state["positions"]))

    def _load_state(self) -> dict:
        """Load or initialize paper trading state."""
        try:
            with open(PAPER_TRADES_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "balance": PAPER_STARTING_BALANCE,
                "orders": [],
                "positions": {},
                "next_order_id": 1,
            }

    def _save_state(self) -> None:
        """Persist paper trading state to disk (atomic write to prevent corruption)."""
        os.makedirs(os.path.dirname(PAPER_TRADES_FILE), exist_ok=True)
        tmp = PAPER_TRADES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp, PAPER_TRADES_FILE)

    def place_order(self, ticker: str, side: str, size: int,
                    order_type: str = "limit", price: int = None) -> dict:
        """
        Simulate placing an order. Fills instantly at the requested price.
        """
        order_id = str(self._state["next_order_id"])
        self._state["next_order_id"] += 1

        cost_per_contract = price if price else 50  # cents
        total_cost = cost_per_contract * size

        if total_cost > self._state["balance"]:
            affordable = self._state["balance"] // cost_per_contract
            if affordable <= 0:
                logger.warning("Paper: insufficient balance for %s", ticker)
                return {"order": {"order_id": order_id, "status": "canceled"}}
            size = affordable
            total_cost = cost_per_contract * size

        self._state["balance"] -= total_cost

        order = {
            "order_id": order_id,
            "ticker": ticker,
            "side": side,
            "count": size,
            "remaining_count": 0,
            "price": cost_per_contract,
            "total_cost": total_cost,
            "status": "filled",
            "type": order_type,
            "created_time": datetime.now(timezone.utc).isoformat(),
        }
        self._state["orders"].append(order)

        # Update positions
        pos_key = f"{ticker}_{side}"
        if pos_key not in self._state["positions"]:
            self._state["positions"][pos_key] = {
                "ticker": ticker,
                "side": side,
                "count": 0,
                "avg_price": 0,
            }
        pos = self._state["positions"][pos_key]
        total_count = pos["count"] + size
        pos["avg_price"] = ((pos["avg_price"] * pos["count"]) + (cost_per_contract * size)) / total_count if total_count else 0
        pos["count"] = total_count

        self._save_state()

        logger.info("Paper order filled: %s %s %d@%dc ($%.2f) | Balance: $%.2f",
                     side.upper(), ticker, size, cost_per_contract,
                     total_cost / 100, self._state["balance"] / 100)

        return {"order": order}

    def get_order(self, order_id: str) -> dict:
        """Return a simulated order by ID."""
        for order in self._state["orders"]:
            if order["order_id"] == order_id:
                return {"order": order}
        return {"order": {"order_id": order_id, "status": "not_found", "remaining_count": 0}}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a paper order (only if not already filled)."""
        for order in self._state["orders"]:
            if order["order_id"] == order_id and order["status"] != "filled":
                order["status"] = "canceled"
                self._save_state()
                return {"order": order}
        return {"order": {"order_id": order_id, "status": "already_filled"}}

    def get_positions(self) -> dict:
        """Return current paper positions."""
        positions = [
            {
                "ticker": pos["ticker"],
                "side": pos["side"],
                "count": pos["count"],
                "avg_price": pos["avg_price"],
            }
            for pos in self._state["positions"].values()
            if pos["count"] > 0
        ]
        return {"market_positions": positions}

    def get_balance(self) -> dict:
        """Return current paper balance."""
        return {"balance": self._state["balance"]}

    def settle_position(self, ticker: str, side: str, outcome: str) -> float:
        """
        Manually settle a paper position for P&L tracking.

        Args:
            ticker: Contract ticker
            side: 'yes' or 'no'
            outcome: 'yes' or 'no' — what actually happened

        Returns:
            P&L in dollars (positive = profit)
        """
        pos_key = f"{ticker}_{side}"
        pos = self._state["positions"].get(pos_key)
        if not pos or pos["count"] == 0:
            return 0.0

        won = (side == outcome)
        if won:
            payout = pos["count"] * 100  # $1 per contract in cents
            pnl_cents = payout - (pos["avg_price"] * pos["count"])
        else:
            pnl_cents = -(pos["avg_price"] * pos["count"])

        self._state["balance"] += (pos["avg_price"] * pos["count"]) + pnl_cents
        pos["count"] = 0

        self._save_state()

        pnl_dollars = pnl_cents / 100
        logger.info("Paper settlement: %s %s → %s | P&L: $%.2f | Balance: $%.2f",
                     side.upper(), ticker, outcome.upper(), pnl_dollars,
                     self._state["balance"] / 100)
        return pnl_dollars
