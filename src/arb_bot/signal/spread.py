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
import math
import sqlite3
import time
from dataclasses import dataclass

from arb_bot.config import Config

log = logging.getLogger(__name__)

# Per-trade fee model. Both venues' fees depend on the leg's price, so a
# single flat-bps constant under/over-states the cost depending on where
# the legs trade. Using actual formulas tightens the threshold and avoids
# false-positive signals on near-50/50 contracts (where flat-bps was too
# generous) and false-negatives on tail contracts (where it was too harsh).
#
# Kalshi standard taker fee per contract (universal across series for
# takers; the `quadratic_with_maker_fees` schedule on NBA only differs
# in whether MAKERS pay -- takers pay the same):
#     fee = 0.07 * P * (1 - P)
# Kalshi rounds this up to the next penny per fill; we omit the ceil to
# keep the signal model continuous and let the slippage knob below absorb
# the small under-estimate.
def _kalshi_taker_fee_per_contract(price: float) -> float:
    """Kalshi taker fee in dollars per contract at the given fill price."""
    p = max(0.0, min(1.0, price))
    return 0.07 * p * (1.0 - p)


# Polymarket Global taker fee per contract: 2% of the contract dollar
# value (0% for makers; arb crosses so we always pay taker). Some markets
# have a market-specific `taker_base_fee` in the gamma payload; default
# 200 bps applied when unset.
POLY_GLOBAL_TAKER_FEE_RATE = 0.02

def _poly_taker_fee_per_contract(price: float) -> float:
    """Polymarket taker fee in dollars per contract at the given fill price."""
    p = max(0.0, min(1.0, price))
    return POLY_GLOBAL_TAKER_FEE_RATE * p


def _round_trip_fee_bps(kal_mid: float, poly_mid: float) -> float:
    """Round-trip fee in basis points of $1 contract notional.

    For an arb pair we cross the book on both legs, so we pay both
    venues' taker fees per contract. Returned in bps so it can be
    subtracted directly from `raw_spread * 10_000`.
    """
    fee_per_contract = (
        _kalshi_taker_fee_per_contract(kal_mid)
        + _poly_taker_fee_per_contract(poly_mid)
    )
    return fee_per_contract * 10_000  # dollars-on-$1-notional -> bps


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
    target_capital_usd: float  # planned $ outlay for this position (informational)
    would_trade: bool
    reject_reason: str | None
    days_to_resolve: float | None = None
    annualized_edge_bps: float | None = None  # edge × 365 / days; capital-efficiency metric


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _round_trip_slippage_bps(cfg: Config) -> float:
    """Per-pair round-trip slippage budget in bps.

    Real fills cross the spread + eat tick-size depth on both legs.
    Paper-mode fills assume mid prices, so we subtract this conservative
    buffer from realized edge before deciding to trade. Tunable via
    SLIPPAGE_BPS_PER_LEG -- default 50 bps/leg = 100 bps round-trip is
    conservative for liquid pairs and tight on thin Polymarket markets.
    Tighten/loosen after live data confirms.
    """
    return 2 * cfg.slippage_bps_per_leg


def _capital_per_unit(polarity: str, kal_mid: float, poly_mid: float) -> float:
    """Capital required per contract-unit traded.

    SAME polarity (buy cheap leg + sell expensive leg):
        capital = max(kal_mid, poly_mid)  ≈ ~$0.50 at-the-money
    INVERSE polarity (sell-both-YES or buy-both-YES):
        capital = kal_mid + poly_mid      ≈ ~$1.00 at-the-money
        (we treat the SHORT-both-YES case as having to deliver $1 if
         either fires — worst-case capital reservation)
    """
    if polarity == "inverse":
        return max(0.01, kal_mid + poly_mid)
    return max(0.01, max(kal_mid, poly_mid))


def _compute_size_units(
    cfg: Config, polarity: str, kal_mid: float, poly_mid: float
) -> tuple[int, float]:
    """Translate the bankroll-fraction target into integer contract units.

    Returns (size_units, target_capital_usd). When the target falls below
    PAPER_MIN_POSITION_USD, returns (0, 0) — caller treats as "skip too
    small".
    """
    target = cfg.paper_per_pair_target_usd
    if target < cfg.paper_min_position_usd:
        return 0, 0.0
    cpu = _capital_per_unit(polarity, kal_mid, poly_mid)
    units = max(1, int(target / cpu))
    actual_capital = units * cpu
    return units, actual_capital


def _detect_same_polarity(
    cfg: Config, pair_id: str, kal_mid: float, poly_mid: float
) -> tuple[float, float, str]:
    """Returns (raw_spread, fee_adjusted_edge_bps, direction).

    Fee model: per-trade Kalshi+Polymarket taker fees evaluated at the
    actual fill prices, plus round-trip slippage budget from cfg.
    """
    raw_spread = abs(kal_mid - poly_mid)
    raw_edge_bps = raw_spread * 10_000
    fee_adjusted_edge_bps = (
        raw_edge_bps
        - _round_trip_fee_bps(kal_mid, poly_mid)
        - _round_trip_slippage_bps(cfg)
    )
    if kal_mid < poly_mid:
        direction = "buy_kalshi_yes_sell_poly_yes"
    elif poly_mid < kal_mid:
        direction = "buy_poly_yes_sell_kalshi_yes"
    else:
        direction = "flat"
    return raw_spread, fee_adjusted_edge_bps, direction


def _detect_inverse_polarity(
    cfg: Config, pair_id: str, kal_mid: float, poly_mid: float
) -> tuple[float, float, str]:
    """Returns (raw_spread, fee_adjusted_edge_bps, direction).

    Fee model: per-trade Kalshi+Polymarket taker fees evaluated at the
    actual fill prices, plus round-trip slippage budget from cfg.
    """
    sum_yes = kal_mid + poly_mid
    raw_spread = abs(sum_yes - 1.0)
    raw_edge_bps = raw_spread * 10_000
    fee_adjusted_edge_bps = (
        raw_edge_bps
        - _round_trip_fee_bps(kal_mid, poly_mid)
        - _round_trip_slippage_bps(cfg)
    )
    if sum_yes > 1.0:
        direction = "sell_both_yes_inverse"
    elif sum_yes < 1.0:
        direction = "buy_both_yes_inverse"
    else:
        direction = "flat"
    return raw_spread, fee_adjusted_edge_bps, direction


def detect_for_pair(conn: sqlite3.Connection, cfg: Config, pair_row: sqlite3.Row) -> ArbSignal | None:
    kal = conn.execute(
        "SELECT yes_bid, yes_ask, last_seen_ts, status, "
        "       resolution_time, close_time "
        "FROM markets WHERE venue='kalshi' AND venue_market_id=?",
        (pair_row["kalshi_ticker"],),
    ).fetchone()
    poly = conn.execute(
        "SELECT yes_bid, yes_ask, last_seen_ts, status, "
        "       resolution_time, close_time "
        "FROM markets WHERE venue='poly_global' AND venue_market_id=?",
        (pair_row["poly_global_market_id"],),
    ).fetchone()
    if not kal or not poly:
        return None

    # Stale-price guard: if either venue's last_seen_ts is older than the
    # configured threshold, refuse to signal. Prevents fake arbs from
    # settled-but-cached or delisted markets where the quote in the DB is
    # no longer reachable on the venue's order book.
    now_ts = int(time.time())
    kal_age = now_ts - (kal["last_seen_ts"] or 0)
    poly_age = now_ts - (poly["last_seen_ts"] or 0)
    if kal_age > cfg.max_quote_age_sec or poly_age > cfg.max_quote_age_sec:
        kal_min = kal_age // 60
        poly_min = poly_age // 60
        return ArbSignal(
            pair_id=pair_row["pair_id"],
            polarity=pair_row["match_polarity"] if "match_polarity" in pair_row.keys() else "unknown",
            kalshi_yes_mid=_mid(kal["yes_bid"], kal["yes_ask"]) or 0.0,
            poly_yes_mid=_mid(poly["yes_bid"], poly["yes_ask"]) or 0.0,
            raw_spread=0.0,
            fee_adjusted_edge_bps=0.0,
            direction="skip_stale_quote",
            size_units=0,
            target_capital_usd=0.0,
            would_trade=False,
            reject_reason=f"stale quote: kal={kal_min}min poly={poly_min}min old "
                          f"(threshold {cfg.max_quote_age_sec // 60}min)",
        )

    # Resolved-market filter: skip pairs where either side's status is
    # not 'open' (closed / resolved / finalized markets).
    if (kal["status"] or "").lower() not in ("open", "active") or \
       (poly["status"] or "").lower() not in ("open", "active"):
        return ArbSignal(
            pair_id=pair_row["pair_id"],
            polarity=pair_row["match_polarity"] if "match_polarity" in pair_row.keys() else "unknown",
            kalshi_yes_mid=_mid(kal["yes_bid"], kal["yes_ask"]) or 0.0,
            poly_yes_mid=_mid(poly["yes_bid"], poly["yes_ask"]) or 0.0,
            raw_spread=0.0,
            fee_adjusted_edge_bps=0.0,
            direction="skip_market_closed",
            size_units=0,
            target_capital_usd=0.0,
            would_trade=False,
            reject_reason=f"market closed/resolved: kal={kal['status']} poly={poly['status']}",
        )

    kal_mid = _mid(kal["yes_bid"], kal["yes_ask"])
    poly_mid = _mid(poly["yes_bid"], poly["yes_ask"])
    if kal_mid is None or poly_mid is None:
        return None

    # Time-to-resolution gate. Prefer Polymarket's close_time (cleaner)
    # then Kalshi's resolution_time, then Kalshi's close_time. If beyond
    # max_days_to_resolve, refuse the signal: capital tied up that long
    # destroys annualized return regardless of edge size (5% in 18 mo
    # = ~3% annualized; 5% in 30 days = ~80%).
    resolve_ts = (
        poly["close_time"]
        or kal["resolution_time"]
        or kal["close_time"]
    )
    days_to_resolve: float | None = None
    if resolve_ts:
        days_to_resolve = max(0.0, (resolve_ts - now_ts) / 86400.0)
        if days_to_resolve > cfg.max_days_to_resolve:
            return ArbSignal(
                pair_id=pair_row["pair_id"],
                polarity=pair_row["match_polarity"] if "match_polarity" in pair_row.keys() else "unknown",
                kalshi_yes_mid=kal_mid,
                poly_yes_mid=poly_mid,
                raw_spread=0.0,
                fee_adjusted_edge_bps=0.0,
                direction="skip_too_long_to_resolve",
                size_units=0,
                target_capital_usd=0.0,
                would_trade=False,
                reject_reason=(
                    f"resolves in {days_to_resolve:.0f}d > "
                    f"max {cfg.max_days_to_resolve}d (capital-locked too long)"
                ),
                days_to_resolve=days_to_resolve,
                annualized_edge_bps=None,
            )

    polarity = (pair_row["match_polarity"] if "match_polarity" in pair_row.keys()
                else "unknown") or "unknown"

    if polarity == "same":
        raw_spread, fee_adjusted_edge_bps, direction = _detect_same_polarity(
            cfg, pair_row["pair_id"], kal_mid, poly_mid
        )
    elif polarity == "inverse":
        raw_spread, fee_adjusted_edge_bps, direction = _detect_inverse_polarity(
            cfg, pair_row["pair_id"], kal_mid, poly_mid
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
            target_capital_usd=0.0,
            would_trade=False,
            reject_reason="polarity unknown — needs re-adjudication or human override",
        )

    # Bankroll-driven sizing
    size_units, target_capital_usd = _compute_size_units(
        cfg, polarity, kal_mid, poly_mid
    )

    would_trade = True
    reject_reason: str | None = None
    if size_units == 0:
        would_trade = False
        reject_reason = (
            f"target ${cfg.paper_per_pair_target_usd:.2f} < min "
            f"${cfg.paper_min_position_usd:.2f} — bankroll too small "
            f"(set INITIAL_BANKROLL_USD higher)"
        )
    if would_trade and fee_adjusted_edge_bps < cfg.paper_min_edge_bps:
        would_trade = False
        reject_reason = (
            f"edge {fee_adjusted_edge_bps:.0f}bps < "
            f"threshold {cfg.paper_min_edge_bps}bps"
        )
    if pair_row["tag"] == "high_risk":
        would_trade = False
        reject_reason = "high_risk pair: excluded from paper v1 trading"

    # Annualized edge = capital-efficiency metric. A 5% edge in 30 days
    # = ~80% APY; same 5% in 18 months = ~3% APY. Used by the signal
    # cycle's display (sort key) and by the dashboard's PnL view.
    annualized_edge_bps: float | None = None
    if days_to_resolve and days_to_resolve > 0:
        annualized_edge_bps = fee_adjusted_edge_bps * 365.0 / days_to_resolve

    return ArbSignal(
        pair_id=pair_row["pair_id"],
        polarity=polarity,
        kalshi_yes_mid=kal_mid,
        poly_yes_mid=poly_mid,
        raw_spread=raw_spread,
        fee_adjusted_edge_bps=fee_adjusted_edge_bps,
        direction=direction,
        size_units=size_units,
        target_capital_usd=target_capital_usd,
        would_trade=would_trade,
        reject_reason=reject_reason,
        days_to_resolve=days_to_resolve,
        annualized_edge_bps=annualized_edge_bps,
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
