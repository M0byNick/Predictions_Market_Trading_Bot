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
    """Naive paper PnL: sum of (sell price - buy price) * size - fees since UTC midnight.

    Phase 2: replace with mark-to-market against live quotes + realized-at-resolution.
    """
    start = _day_start_ts()
    fills = conn.execute(
        """
        SELECT leg, side, price_filled, size_filled, fees_usd
        FROM paper_fills WHERE ts >= ? AND state = 'filled'
        """,
        (start,),
    ).fetchall()
    pnl = 0.0
    for f in fills:
        direction = 1.0 if f["side"] == "sell" else -1.0
        pnl += direction * f["price_filled"] * f["size_filled"]
        pnl -= f["fees_usd"] or 0.0
    return pnl


def open_position_usd(conn: sqlite3.Connection, pair_id: str) -> float:
    """Approx open notional on a pair: sum of |size_filled * price_filled|
    across unsettled fills. Placeholder until settlement tracking exists.
    """
    rows = conn.execute(
        "SELECT price_filled, size_filled FROM paper_fills WHERE pair_id=? AND state='filled'",
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
