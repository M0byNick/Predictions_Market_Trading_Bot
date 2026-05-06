import logging
import sqlite3
import time

from arb_bot.config import Config

log = logging.getLogger(__name__)


def _day_start_ts() -> int:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp())


def daily_pnl_usd(conn: sqlite3.Connection) -> float:
    """Realized paper PnL since UTC midnight.

    Sums realized_pnl_usd from all paper_fills SETTLED today. Open
    positions don't contribute (their realized_pnl_usd is NULL until
    settle_paper_fills.py picks them up).
    """
    start = _day_start_ts()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(realized_pnl_usd), 0.0) AS pnl
        FROM paper_fills
        WHERE settled_ts >= ? AND realized_outcome IS NOT NULL
        """,
        (start,),
    ).fetchone()
    return row["pnl"] if row else 0.0


def open_position_usd(conn: sqlite3.Connection, pair_id: str) -> float:
    """Open notional on a pair: sum of |size_filled * price_filled| across
    UNSETTLED fills only. Settled positions (realized_outcome set) no
    longer occupy capital; the bot can re-enter the pair on the next
    cycle if a fresh signal appears.
    """
    rows = conn.execute(
        "SELECT price_filled, size_filled FROM paper_fills "
        "WHERE pair_id=? AND state='filled' AND realized_outcome IS NULL",
        (pair_id,),
    ).fetchall()
    return sum((r["price_filled"] or 0) * (r["size_filled"] or 0) for r in rows)


def check(conn: sqlite3.Connection, cfg: Config, pair_id: str) -> tuple[bool, str | None]:
    """Return (ok, reason_if_not_ok). Called just before simulate_fill."""
    pnl = daily_pnl_usd(conn)
    if pnl <= -cfg.paper_daily_max_loss_usd:
        return False, f"daily_pnl {pnl:.2f} <= -{cfg.paper_daily_max_loss_usd} (daily stop)"
    pos = open_position_usd(conn, pair_id)
    if pos >= cfg.paper_max_position_usd:
        return False, f"open_position {pos:.2f} >= {cfg.paper_max_position_usd}"
    return True, None
