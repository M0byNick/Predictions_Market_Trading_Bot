import logging
import sqlite3
import time
from dataclasses import dataclass

from arb_bot.config import Config

log = logging.getLogger(__name__)

# Fee assumptions (paper; tune when live venue data is connected).
# Kalshi fees scale with price — conservative flat rate for paper.
KALSHI_FEE_BPS = 70  # ~0.7% Kalshi taker side, paper conservative estimate
POLY_GLOBAL_FEE_BPS = 200  # 2.0% Polymarket Global TAKER fee (0% maker; arb generally crosses)


@dataclass
class ArbSignal:
    pair_id: str
    kalshi_yes_mid: float
    poly_yes_mid: float
    raw_spread: float
    fee_adjusted_edge_bps: float
    direction: str
    size_units: int
    would_trade: bool
    reject_reason: str | None


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _sum_fee_bps() -> int:
    # Round trip = buy one side + sell (or symmetric buy) the other. Both legs charged.
    return KALSHI_FEE_BPS + POLY_GLOBAL_FEE_BPS


def detect_for_pair(conn: sqlite3.Connection, cfg: Config, pair_row: sqlite3.Row) -> ArbSignal | None:
    kal = conn.execute(
        "SELECT yes_bid, yes_ask FROM markets WHERE venue='kalshi' AND venue_market_id=?",
        (pair_row["kalshi_ticker"],),
    ).fetchone()
    poly = conn.execute(
        "SELECT yes_bid, yes_ask FROM markets WHERE venue='poly_global' AND venue_market_id=?",
        (pair_row["poly_global_market_id"],),
    ).fetchone()
    if not kal or not poly:
        return None

    kal_mid = _mid(kal["yes_bid"], kal["yes_ask"])
    poly_mid = _mid(poly["yes_bid"], poly["yes_ask"])
    if kal_mid is None or poly_mid is None:
        return None

    raw_spread = abs(kal_mid - poly_mid)
    # Edge in bps of $1 contract notional
    raw_edge_bps = raw_spread * 10_000
    fee_adjusted_edge_bps = raw_edge_bps - _sum_fee_bps()

    # Direction: buy the cheaper YES, sell the more expensive YES
    if kal_mid < poly_mid:
        direction = "buy_kalshi_yes_sell_poly_yes"
    elif poly_mid < kal_mid:
        direction = "buy_poly_yes_sell_kalshi_yes"
    else:
        direction = "flat"

    would_trade = True
    reject_reason = None
    if fee_adjusted_edge_bps < cfg.paper_min_edge_bps:
        would_trade = False
        reject_reason = f"edge {fee_adjusted_edge_bps:.0f}bps < threshold {cfg.paper_min_edge_bps}bps"
    if pair_row["tag"] == "high_risk":
        would_trade = False
        reject_reason = "high_risk pair: excluded from paper v1 trading"

    size_units = 10  # paper v1: fixed 10-unit size; risk layer may shrink further

    return ArbSignal(
        pair_id=pair_row["pair_id"],
        kalshi_yes_mid=kal_mid,
        poly_yes_mid=poly_mid,
        raw_spread=raw_spread,
        fee_adjusted_edge_bps=fee_adjusted_edge_bps,
        direction=direction,
        size_units=size_units,
        would_trade=would_trade,
        reject_reason=reject_reason,
    )


def scan_all(conn: sqlite3.Connection, cfg: Config) -> list[ArbSignal]:
    pairs = conn.execute(
        "SELECT * FROM approved_pairs WHERE active=1"
    ).fetchall()
    out = []
    for p in pairs:
        sig = detect_for_pair(conn, cfg, p)
        if sig:
            out.append(sig)
    return out


def record_signal(conn: sqlite3.Connection, sig: ArbSignal) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO paper_signals
        (pair_id, detected_ts, kalshi_yes_mid, poly_yes_mid, raw_spread,
         fee_adjusted_edge_bps, direction, size_units, would_trade, reject_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sig.pair_id,
            now_ts,
            sig.kalshi_yes_mid,
            sig.poly_yes_mid,
            sig.raw_spread,
            sig.fee_adjusted_edge_bps,
            sig.direction,
            sig.size_units,
            1 if sig.would_trade else 0,
            sig.reject_reason,
        ),
    )
    return cur.lastrowid
