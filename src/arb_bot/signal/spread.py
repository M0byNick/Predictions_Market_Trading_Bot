"""Cross-venue arbitrage signal detector.

There are TWO arb regimes depending on `approved_pairs.match_polarity`:

──────────────────────────────────────────────────────────────────────
SAME-POLARITY (Kalshi YES ≡ Poly YES, same real-world outcome)
──────────────────────────────────────────────────────────────────────
  At fair value:    kalshi_yes_price == poly_yes_price
  Edge per $1 ntl:  |kalshi_yes - poly_yes|
  When kal < poly:  BUY  Kalshi YES  +  SELL Poly YES   (both legs in $)
  When poly < kal:  BUY  Poly YES    +  SELL Kalshi YES
  Net cash now:     +(higher - lower) per pair
  Net at expiry:    0 (one leg pays $1, other costs $1)
  Profit:           +(higher - lower) - fees - slippage
  Direction tags:   "buy_kalshi_yes_sell_poly_yes" / "buy_poly_yes_sell_kalshi_yes"

──────────────────────────────────────────────────────────────────────
INVERSE-POLARITY (Kalshi YES ≡ Poly NO, complementary outcomes)
──────────────────────────────────────────────────────────────────────
  At fair value:    kalshi_yes_price + poly_yes_price == 1.00
  Edge per $1 ntl:  |kalshi_yes + poly_yes - 1.00|
  When sum > $1:    SELL both YES (or BUY both NO)  — cap = sum, owe $1
  When sum < $1:    BUY  both YES                   — pay sum, receive $1
  Direction tags:   "sell_both_yes_inverse" / "buy_both_yes_inverse"

  CAVEAT: Inverse arb assumes a 2-outcome universe. In Dem-vs-Rep
  governorship pairs, an Independent / write-in win would resolve
  BOTH legs to NO, breaking the arb. We accept this tail because
  third-party wins are rare in modern US gubernatorial elections;
  the dashboard surfaces the polarity before approval so the
  reviewer can refuse pairs where third-party risk is real.

──────────────────────────────────────────────────────────────────────
UNKNOWN POLARITY
──────────────────────────────────────────────────────────────────────
  Refuse to trade. Reject reason: "polarity unknown — skipped".
  These pairs need either re-adjudication (LLM with new prompt) or
  manual override via the dashboard's polarity radio.
"""
import logging
import sqlite3
import time
from dataclasses import dataclass

from arb_bot.config import Config

log = logging.getLogger(__name__)

# Fee assumptions (paper; tune when live venue data is connected).
KALSHI_FEE_BPS = 70  # ~0.7% Kalshi taker side, paper conservative estimate
POLY_GLOBAL_FEE_BPS = 200  # 2.0% Polymarket Global TAKER fee (0% maker; arb generally crosses)


@dataclass
class ArbSignal:
    pair_id: str
    polarity: str  # 'same' | 'inverse' | 'unknown'
    kalshi_yes_mid: float
    poly_yes_mid: float
    raw_spread: float          # |kal - poly| for same; |kal + poly - 1| for inverse
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
    """Round-trip fee assumption: both legs charged on each venue."""
    return KALSHI_FEE_BPS + POLY_GLOBAL_FEE_BPS


def _detect_same_polarity(
    pair_id: str, kal_mid: float, poly_mid: float
) -> tuple[float, float, str]:
    """Returns (raw_spread, fee_adjusted_edge_bps, direction)."""
    raw_spread = abs(kal_mid - poly_mid)
    raw_edge_bps = raw_spread * 10_000
    fee_adjusted_edge_bps = raw_edge_bps - _sum_fee_bps()
    if kal_mid < poly_mid:
        direction = "buy_kalshi_yes_sell_poly_yes"
    elif poly_mid < kal_mid:
        direction = "buy_poly_yes_sell_kalshi_yes"
    else:
        direction = "flat"
    return raw_spread, fee_adjusted_edge_bps, direction


def _detect_inverse_polarity(
    pair_id: str, kal_mid: float, poly_mid: float
) -> tuple[float, float, str]:
    """Returns (raw_spread, fee_adjusted_edge_bps, direction)."""
    sum_yes = kal_mid + poly_mid
    raw_spread = abs(sum_yes - 1.0)
    raw_edge_bps = raw_spread * 10_000
    fee_adjusted_edge_bps = raw_edge_bps - _sum_fee_bps()
    if sum_yes > 1.0:
        direction = "sell_both_yes_inverse"
    elif sum_yes < 1.0:
        direction = "buy_both_yes_inverse"
    else:
        direction = "flat"
    return raw_spread, fee_adjusted_edge_bps, direction


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

    polarity = (pair_row["match_polarity"] if "match_polarity" in pair_row.keys()
                else "unknown") or "unknown"

    if polarity == "same":
        raw_spread, fee_adjusted_edge_bps, direction = _detect_same_polarity(
            pair_row["pair_id"], kal_mid, poly_mid
        )
    elif polarity == "inverse":
        raw_spread, fee_adjusted_edge_bps, direction = _detect_inverse_polarity(
            pair_row["pair_id"], kal_mid, poly_mid
        )
    else:
        # unknown — refuse to compute a signal at all
        return ArbSignal(
            pair_id=pair_row["pair_id"],
            polarity="unknown",
            kalshi_yes_mid=kal_mid,
            poly_yes_mid=poly_mid,
            raw_spread=0.0,
            fee_adjusted_edge_bps=0.0,
            direction="skip_polarity_unknown",
            size_units=0,
            would_trade=False,
            reject_reason="polarity unknown — needs re-adjudication or human override",
        )

    would_trade = True
    reject_reason: str | None = None
    if fee_adjusted_edge_bps < cfg.paper_min_edge_bps:
        would_trade = False
        reject_reason = f"edge {fee_adjusted_edge_bps:.0f}bps < threshold {cfg.paper_min_edge_bps}bps"
    if pair_row["tag"] == "high_risk":
        would_trade = False
        reject_reason = "high_risk pair: excluded from paper v1 trading"

    size_units = 10  # paper v1: fixed 10-unit size; risk layer may shrink further

    return ArbSignal(
        pair_id=pair_row["pair_id"],
        polarity=polarity,
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
