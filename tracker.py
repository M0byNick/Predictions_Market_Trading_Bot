"""
Trade tracker for logging, performance measurement, and edge detection.

Every trade gets logged with your estimated probability, the market price,
sizing, and outcome. Over time, this data tells you whether you actually
have an edge or are just getting lucky (or unlucky).

Key metrics:
  - Brier score: measures calibration (lower = better)
  - Hit rate: percentage of trades where you were on the correct side
  - Edge realized: average (your_prob - market_prob) on winning trades
  - Bankroll curve: cumulative P&L over time
"""
import json
import os
import csv
from datetime import datetime, timezone
from typing import Optional
import config
from log import logger


PENDING_ORDERS_FILE = "data/pending_orders.json"


class Tracker:
    """Manages the trade log and computes performance metrics."""

    def __init__(self, trades_file: str = None, performance_file: str = None,
                 pending_file: str = None):
        self.trades_file = trades_file or config.TRADES_FILE
        self.performance_file = performance_file or config.PERFORMANCE_FILE
        self.pending_file = pending_file or PENDING_ORDERS_FILE
        self.trades = self._load_trades()

    def _load_trades(self) -> list:
        """Load existing trades from the JSON file, or start fresh."""
        if os.path.exists(self.trades_file):
            with open(self.trades_file, "r") as f:
                return json.load(f)
        return []

    def _save_trades(self):
        """Persist the trade log to disk (atomic write to prevent corruption)."""
        os.makedirs(os.path.dirname(self.trades_file), exist_ok=True)
        tmp = self.trades_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.trades, f, indent=2, default=str)
        os.replace(tmp, self.trades_file)

    # ── Pending Orders (crash recovery) ─────────────────────────────────

    def _load_pending(self) -> list:
        """Load pending orders from disk."""
        try:
            with open(self.pending_file, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_pending(self, pending: list) -> None:
        """Persist pending orders to disk (atomic)."""
        os.makedirs(os.path.dirname(self.pending_file), exist_ok=True)
        tmp = self.pending_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pending, f, indent=2, default=str)
        os.replace(tmp, self.pending_file)

    def mark_pending(self, ticker: str, side: str, contracts: int,
                     cost_usd: float, order_id: str = None) -> None:
        """Record an order as pending before execution."""
        pending = self._load_pending()
        pending.append({
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "cost_usd": cost_usd,
            "order_id": order_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._save_pending(pending)

    def clear_pending(self, ticker: str, side: str) -> None:
        """Remove a pending order after it has been tracked."""
        pending = self._load_pending()
        pending = [p for p in pending
                   if not (p["ticker"] == ticker and p["side"] == side)]
        self._save_pending(pending)

    def get_pending_orders(self) -> list:
        """Return all pending orders for reconciliation."""
        return self._load_pending()

    # ── Trade Logging ─────────────────────────────────────────────────────

    def log_trade(self, ticker: str, category: str, side: str,
                  your_prob: float, market_prob: float,
                  num_contracts: int, cost_usd: float,
                  kelly_fraction: float, notes: str = "") -> dict:
        """
        Log a new trade at entry time. The outcome will be recorded
        later when the contract settles.
        """
        trade = {
            "id": len(self.trades) + 1,
            "ticker": ticker,
            "category": category,
            "side": side,
            "your_prob": your_prob,
            "market_prob": market_prob,
            "edge_at_entry": round(your_prob - market_prob, 4) if side == "yes"
                             else round((1 - your_prob) - (1 - market_prob), 4),
            "num_contracts": num_contracts,
            "cost_usd": cost_usd,
            "kelly_fraction": kelly_fraction,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "outcome": None,        # "win" or "loss" — filled at settlement
            "settlement_price": None,  # 1.0 or 0.0 for binary contracts
            "pnl_usd": None,
            "notes": notes,
        }
        self.trades.append(trade)
        self._save_trades()
        return trade

    def record_outcome(self, trade_id: int, outcome: str, settlement_price: float = None):
        """
        Record the outcome of a settled trade. Call this when a Kalshi
        contract resolves.

        Args:
            trade_id: The trade's ID from log_trade().
            outcome: 'win' or 'loss'.
            settlement_price: 1.0 if the event happened, 0.0 if not.
        """
        for trade in self.trades:
            if trade["id"] == trade_id:
                trade["outcome"] = outcome
                trade["settlement_price"] = settlement_price

                # Calculate P&L. If you bought YES at 0.62 and it settles YES:
                # profit = (1.0 - 0.62) * num_contracts = $0.38 per contract
                # If it settles NO: loss = -0.62 * num_contracts
                cost_per = trade["market_prob"] if trade["side"] == "yes" else (1 - trade["market_prob"])
                if outcome == "win":
                    pnl = (1.0 - cost_per) * trade["num_contracts"]
                else:
                    pnl = -cost_per * trade["num_contracts"]
                trade["pnl_usd"] = round(pnl, 2)
                trade["settlement_time"] = datetime.now(timezone.utc).isoformat()
                break

        self._save_trades()

    # ── Performance Metrics ──────────────────────────────────────────────

    def settled_trades(self, category: Optional[str] = None) -> list:
        """Get all trades that have been settled (have outcomes)."""
        trades = [t for t in self.trades if t["outcome"] is not None]
        if category:
            trades = [t for t in trades if t["category"] == category]
        return trades

    def hit_rate(self, category: Optional[str] = None) -> float:
        """Percentage of trades that were winners."""
        settled = self.settled_trades(category)
        if not settled:
            return 0.0
        wins = sum(1 for t in settled if t["outcome"] == "win")
        return wins / len(settled)

    def total_pnl(self, category: Optional[str] = None) -> float:
        """Cumulative P&L in USD across settled trades."""
        settled = self.settled_trades(category)
        return sum(t.get("pnl_usd", 0) or 0 for t in settled)

    def average_edge(self, category: Optional[str] = None) -> float:
        """Average edge at entry across all trades (not just winners)."""
        settled = self.settled_trades(category)
        if not settled:
            return 0.0
        return sum(t["edge_at_entry"] for t in settled) / len(settled)

    def brier_score(self, category: Optional[str] = None) -> float:
        """
        Brier score measures calibration. It's the mean squared difference
        between your predicted probabilities and actual outcomes.
        
        A Brier score of 0.0 is perfect. 0.25 is random (coin flip).
        Anything below 0.20 on prediction markets is quite good.
        """
        settled = self.settled_trades(category)
        if not settled:
            return None
        total = 0.0
        for t in settled:
            actual = 1.0 if t["outcome"] == "win" else 0.0
            predicted = t["your_prob"] if t["side"] == "yes" else (1 - t["your_prob"])
            total += (predicted - actual) ** 2
        return total / len(settled)

    def bankroll_curve(self) -> list:
        """
        Compute the cumulative bankroll over time.
        Returns a list of (trade_id, cumulative_pnl) tuples.
        Useful for plotting your growth (or drawdowns).
        """
        settled = sorted(
            self.settled_trades(),
            key=lambda t: t.get("settlement_time", t["entry_time"])
        )
        cumulative = 0.0
        curve = []
        for t in settled:
            cumulative += t.get("pnl_usd", 0) or 0
            curve.append({"trade_id": t["id"], "cumulative_pnl": round(cumulative, 2),
                          "time": t.get("settlement_time", t["entry_time"])})
        return curve

    def summary(self) -> str:
        """Generate a human-readable performance summary."""
        total = len(self.trades)
        settled = self.settled_trades()
        n_settled = len(settled)

        lines = [
            "═══ Portfolio Performance ═══",
            f"Total trades: {total} ({n_settled} settled, {total - n_settled} open)",
            f"Overall hit rate: {self.hit_rate():.1%}",
            f"Total P&L: ${self.total_pnl():.2f}",
            f"Average edge at entry: {self.average_edge():.1%}",
        ]

        brier = self.brier_score()
        if brier is not None:
            lines.append(f"Brier score: {brier:.4f} (lower is better, 0.25 = random)")

        # Per-category breakdown
        for cat in ["crypto", "weather", "economics"]:
            cat_settled = self.settled_trades(cat)
            if cat_settled:
                lines.append(f"\n── {cat.upper()} ──")
                lines.append(f"  Trades: {len(cat_settled)}")
                lines.append(f"  Hit rate: {self.hit_rate(cat):.1%}")
                lines.append(f"  P&L: ${self.total_pnl(cat):.2f}")
                lines.append(f"  Avg edge: {self.average_edge(cat):.1%}")

        return "\n".join(lines)

    def export_csv(self):
        """Export all trades to CSV for spreadsheet analysis."""
        os.makedirs(os.path.dirname(self.performance_file), exist_ok=True)
        if not self.trades:
            return
        fieldnames = list(self.trades[0].keys())
        with open(self.performance_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.trades)
