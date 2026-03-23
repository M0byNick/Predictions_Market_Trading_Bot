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

Storage: SQLite database (migrated from JSON in Tier 2).
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import Optional

import config
from db import init_db, migrate_json_trades, migrate_json_pending, transaction
from log import logger


class Tracker:
    """Manages the trade log and computes performance metrics."""

    def __init__(self, db_path: str = None, performance_file: str = None,
                 trades_file: str = None, pending_file: str = None):
        self.db_path = db_path or config.DB_PATH
        self.performance_file = performance_file or config.PERFORMANCE_FILE
        self._legacy_trades_file = trades_file or config.TRADES_FILE
        self._legacy_pending_file = pending_file or "data/pending_orders.json"
        self.conn = init_db(self.db_path)

        # Auto-migrate legacy JSON data on first run
        migrate_json_trades(self.conn, self._legacy_trades_file)
        migrate_json_pending(self.conn, self._legacy_pending_file)

    # ── Pending Orders (crash recovery) ─────────────────────────────────

    def mark_pending(self, ticker: str, side: str, contracts: int,
                     cost_usd: float, order_id: str = None) -> None:
        """Record an order as pending before execution."""
        self.conn.execute("""
            INSERT INTO pending_orders (ticker, side, contracts, cost_usd,
                order_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (ticker, side, contracts, cost_usd, order_id,
              datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def clear_pending(self, ticker: str, side: str) -> None:
        """Remove a pending order after it has been tracked."""
        self.conn.execute(
            "DELETE FROM pending_orders WHERE ticker = ? AND side = ?",
            (ticker, side),
        )
        self.conn.commit()

    def get_pending_orders(self) -> list:
        """Return all pending orders for reconciliation."""
        rows = self.conn.execute("SELECT * FROM pending_orders").fetchall()
        return [dict(r) for r in rows]

    # ── Trade Logging ─────────────────────────────────────────────────────

    def log_trade(self, ticker: str, category: str, side: str,
                  your_prob: float, market_prob: float,
                  num_contracts: int, cost_usd: float,
                  kelly_fraction: float, notes: str = "",
                  full_kelly: float = None, fractional_kelly: float = None,
                  kelly_rec_usd: float = None,
                  kelly_multiplier: float = None) -> dict:
        """
        Log a new trade at entry time. The outcome will be recorded
        later when the contract settles.

        Kelly columns:
          full_kelly: True Kelly fraction (f* = edge / (1 - market_prob))
          fractional_kelly: full_kelly * KELLY_FRACTION (e.g. quarter-Kelly)
          kelly_rec_usd: Dollar size Kelly recommends at current bankroll
          kelly_multiplier: Ratio of actual cost to kelly_rec_usd (scale-down factor)
        """
        edge = round(your_prob - market_prob, 4) if side == "yes" \
            else round((1 - your_prob) - (1 - market_prob), 4)
        entry_time = datetime.now(timezone.utc).isoformat()

        cursor = self.conn.execute("""
            INSERT INTO trades (ticker, category, side, your_prob, market_prob,
                edge_at_entry, num_contracts, cost_usd, kelly_fraction,
                full_kelly, fractional_kelly, kelly_rec_usd, kelly_multiplier,
                entry_time, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, category, side, your_prob, market_prob, edge,
              num_contracts, cost_usd, kelly_fraction,
              full_kelly, fractional_kelly, kelly_rec_usd, kelly_multiplier,
              entry_time, notes))
        self.conn.commit()

        trade_id = cursor.lastrowid

        return {
            "id": trade_id,
            "ticker": ticker,
            "category": category,
            "side": side,
            "your_prob": your_prob,
            "market_prob": market_prob,
            "edge_at_entry": edge,
            "num_contracts": num_contracts,
            "cost_usd": cost_usd,
            "kelly_fraction": kelly_fraction,
            "full_kelly": full_kelly,
            "fractional_kelly": fractional_kelly,
            "kelly_rec_usd": kelly_rec_usd,
            "kelly_multiplier": kelly_multiplier,
            "entry_time": entry_time,
            "outcome": None,
            "settlement_price": None,
            "pnl_usd": None,
            "notes": notes,
        }

    def record_outcome(self, trade_id: int, outcome: str,
                       settlement_price: float = None):
        """
        Record the outcome of a settled trade.

        Args:
            trade_id: The trade's ID from log_trade().
            outcome: 'win' or 'loss'.
            settlement_price: 1.0 if the event happened, 0.0 if not.
        """
        row = self.conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if row is None:
            return

        trade = dict(row)
        cost_per = trade["market_prob"] if trade["side"] == "yes" \
            else (1 - trade["market_prob"])
        if outcome == "win":
            pnl = (1.0 - cost_per) * trade["num_contracts"]
        else:
            pnl = -cost_per * trade["num_contracts"]
        pnl = round(pnl, 2)

        settlement_time = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            UPDATE trades SET outcome = ?, settlement_price = ?,
                pnl_usd = ?, settlement_time = ?
            WHERE id = ?
        """, (outcome, settlement_price, pnl, settlement_time, trade_id))
        self.conn.commit()

        # Update daily P&L
        date_str = settlement_time[:10]
        self._update_daily_pnl(date_str, trade["category"], pnl, outcome)

    def _update_daily_pnl(self, date_str: str, category: str,
                          pnl: float, outcome: str) -> None:
        """Update the daily P&L aggregation table."""
        win_inc = 1 if outcome == "win" else 0
        loss_inc = 1 if outcome == "loss" else 0

        self.conn.execute("""
            INSERT INTO daily_pnl (date, category, realized_pnl, trade_count,
                win_count, loss_count)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(date, category) DO UPDATE SET
                realized_pnl = realized_pnl + excluded.realized_pnl,
                trade_count = trade_count + 1,
                win_count = win_count + excluded.win_count,
                loss_count = loss_count + excluded.loss_count
        """, (date_str, category, pnl, win_inc, loss_inc))
        self.conn.commit()

    # ── Performance Metrics ──────────────────────────────────────────────

    def settled_trades(self, category: Optional[str] = None) -> list:
        """Get all trades that have been settled (have outcomes)."""
        if category:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE outcome IS NOT NULL AND category = ?",
                (category,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE outcome IS NOT NULL"
            ).fetchall()
        return [dict(r) for r in rows]

    @property
    def trades(self) -> list:
        """All trades (for backward compat with tests and summary)."""
        rows = self.conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
        return [dict(r) for r in rows]

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
            key=lambda t: t.get("settlement_time") or t["entry_time"]
        )
        cumulative = 0.0
        curve = []
        for t in settled:
            cumulative += t.get("pnl_usd", 0) or 0
            curve.append({"trade_id": t["id"], "cumulative_pnl": round(cumulative, 2),
                          "time": t.get("settlement_time") or t["entry_time"]})
        return curve

    def get_daily_pnl(self, days: int = 14) -> list:
        """Get daily P&L records for the last N days."""
        rows = self.conn.execute("""
            SELECT date, category, realized_pnl, trade_count, win_count, loss_count
            FROM daily_pnl
            ORDER BY date DESC
            LIMIT ?
        """, (days * 3,)).fetchall()  # *3 for 3 categories
        return [dict(r) for r in rows]

    def summary(self) -> str:
        """Generate a human-readable performance summary."""
        all_trades = self.trades
        total = len(all_trades)
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
        all_trades = self.trades
        if not all_trades:
            return
        fieldnames = list(all_trades[0].keys())
        with open(self.performance_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_trades)
