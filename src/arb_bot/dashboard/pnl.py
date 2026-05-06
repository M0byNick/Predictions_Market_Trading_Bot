"""Paper-trading PnL aggregation for the /pnl dashboard route.

Computes realized + unrealized PnL across all paper_fills, walking
each fill against current markets-table prices. Excludes the
WIPED_PHANTOM_GAMMA cohort from headline numbers (that wipe was a
data-integrity action, not a real trade outcome).

PnL math per fill (paper convention):

  BUY leg:   entry_cost   = price_filled * size + fees_paid
             current_value = current_yes_mid * size
             unrealized   = current_value - entry_cost

  SELL leg:  cash_received = price_filled * size - fees_paid
             close_cost    = current_yes_mid * size
             unrealized    = cash_received - close_cost

Per-pair PnL = sum across both legs. Mid-based mark-to-market is the
display convention; "executable close" pnl (using bid for closing longs,
ask for closing shorts) would be more conservative — it's surfaced as
a second column for honesty.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


# Tag used by phantom-fill wipes (run when audit gaps were found).
# These rows have realized_pnl_usd=0 + settled_ts set, but they aren't
# real trade outcomes and shouldn't show in PnL totals.
WIPE_TAGS = ("WIPED_PHANTOM_GAMMA",)


@dataclass
class PositionRow:
    pair_id: str
    kalshi_ticker: str
    poly_cid: str
    title: str

    # Entry side (taken from paper_fills)
    n_legs: int
    entry_capital_usd: float       # sum of price_filled*size for BUY legs
    fees_paid_usd: float
    entry_ts: int                  # earliest fill ts for the pair
    direction: str                 # from first fill row

    # Per-leg snapshot prices
    k_entry_price: float | None
    k_entry_side: str | None
    p_entry_price: float | None
    p_entry_side: str | None

    # Mark-to-market snapshot
    k_yes_bid: float | None
    k_yes_ask: float | None
    p_yes_bid: float | None
    p_yes_ask: float | None

    # PnL (computed)
    unrealized_mid: float          # mid-based mtm
    unrealized_executable: float   # close-at-unfavorable-side mtm
    days_open: float
    days_to_resolve: float | None
    close_time: int | None

    @property
    def k_mid(self) -> float | None:
        if self.k_yes_bid is None or self.k_yes_ask is None:
            return None
        return (self.k_yes_bid + self.k_yes_ask) / 2.0

    @property
    def p_mid(self) -> float | None:
        if self.p_yes_bid is None or self.p_yes_ask is None:
            return None
        return (self.p_yes_bid + self.p_yes_ask) / 2.0


def _fill_pnl_mid(side: str, price_filled: float, size: int,
                  fees: float, current_mid: float) -> float:
    """Mid-based mtm pnl for a single fill leg."""
    if side == "buy":
        return current_mid * size - (price_filled * size + fees)
    elif side == "sell":
        return (price_filled * size - fees) - current_mid * size
    return 0.0


def _fill_pnl_executable(
    side: str, price_filled: float, size: int, fees: float,
    yes_bid: float | None, yes_ask: float | None,
) -> float | None:
    """Conservative mtm: close longs at bid, close shorts at ask.

    Returns None if the close-side price is unavailable.
    """
    if side == "buy":
        if yes_bid is None:
            return None
        return yes_bid * size - (price_filled * size + fees)
    elif side == "sell":
        if yes_ask is None:
            return None
        return (price_filled * size - fees) - yes_ask * size
    return 0.0


def compute_pnl_state(conn: sqlite3.Connection) -> dict:
    """Walk paper_fills, compute realized + unrealized PnL aggregates.

    Returns a dict consumed by templates/pnl.html.
    """
    now_ts = int(time.time())

    # ── 1. Realized PnL totals (settled fills, excluding wipes) ─────────
    fills_status = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN realized_outcome IS NULL THEN 1 ELSE 0 END) AS open_n,
          SUM(CASE WHEN realized_outcome IN ({",".join("?" * len(WIPE_TAGS))}) THEN 1 ELSE 0 END) AS wiped_n,
          SUM(CASE WHEN realized_outcome IS NOT NULL
                    AND realized_outcome NOT IN ({",".join("?" * len(WIPE_TAGS))})
                  THEN 1 ELSE 0 END) AS settled_n,
          COUNT(*) AS total_n
        FROM paper_fills
        """,
        WIPE_TAGS + WIPE_TAGS,
    ).fetchone()

    realized_total = conn.execute(
        f"""
        SELECT COALESCE(SUM(realized_pnl_usd), 0)
        FROM paper_fills
        WHERE realized_outcome IS NOT NULL
          AND realized_outcome NOT IN ({",".join("?" * len(WIPE_TAGS))})
        """,
        WIPE_TAGS,
    ).fetchone()[0]

    realized_today = conn.execute(
        f"""
        SELECT COALESCE(SUM(realized_pnl_usd), 0)
        FROM paper_fills
        WHERE settled_ts >= strftime('%s', 'now', 'start of day')
          AND realized_outcome IS NOT NULL
          AND realized_outcome NOT IN ({",".join("?" * len(WIPE_TAGS))})
        """,
        WIPE_TAGS,
    ).fetchone()[0]

    # ── 2. Per-pair open-position roll-up ──────────────────────────────
    # Group open fills by pair_id. For each pair, fetch both legs' current
    # quotes from markets table, compute mtm.
    pair_groups: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute(
        """
        SELECT pf.id, pf.pair_id, pf.leg, pf.side, pf.price_filled,
               pf.size_filled, pf.fees_usd, pf.ts, pf.signal_id,
               ps.direction
          FROM paper_fills pf
          LEFT JOIN paper_signals ps ON ps.id = pf.signal_id
         WHERE pf.realized_outcome IS NULL
        ORDER BY pf.pair_id, pf.id
        """
    ):
        pair_groups.setdefault(r["pair_id"], []).append(r)

    open_positions: list[dict] = []
    total_unrealized_mid = 0.0
    total_unrealized_exec = 0.0
    total_open_capital = 0.0

    for pair_id, fills in pair_groups.items():
        # Resolve current prices once per pair
        ap = conn.execute(
            """
            SELECT ap.kalshi_ticker, ap.poly_global_market_id,
                   km.title AS k_title, km.yes_bid AS k_bid, km.yes_ask AS k_ask,
                   km.close_time AS k_close,
                   pm.title AS p_title, pm.yes_bid AS p_bid, pm.yes_ask AS p_ask,
                   pm.close_time AS p_close
              FROM approved_pairs ap
              LEFT JOIN markets km ON km.venue='kalshi'      AND km.venue_market_id=ap.kalshi_ticker
              LEFT JOIN markets pm ON pm.venue='poly_global' AND pm.venue_market_id=ap.poly_global_market_id
             WHERE ap.pair_id=?
            """,
            (pair_id,),
        ).fetchone()
        if not ap:
            continue
        k_mid = ((ap["k_bid"] or 0) + (ap["k_ask"] or 0)) / 2.0 if ap["k_bid"] is not None else None
        p_mid = ((ap["p_bid"] or 0) + (ap["p_ask"] or 0)) / 2.0 if ap["p_bid"] is not None else None

        unrealized_mid = 0.0
        unrealized_exec = 0.0
        entry_capital = 0.0
        fees_paid = 0.0
        earliest_ts = min(f["ts"] for f in fills)
        k_entry_price, k_entry_side = None, None
        p_entry_price, p_entry_side = None, None

        for f in fills:
            current_mid = k_mid if f["leg"] == "kalshi" else p_mid
            cur_bid = ap["k_bid"] if f["leg"] == "kalshi" else ap["p_bid"]
            cur_ask = ap["k_ask"] if f["leg"] == "kalshi" else ap["p_ask"]

            if current_mid is not None:
                unrealized_mid += _fill_pnl_mid(
                    f["side"], f["price_filled"], f["size_filled"],
                    f["fees_usd"] or 0, current_mid,
                )
                pnl_exec = _fill_pnl_executable(
                    f["side"], f["price_filled"], f["size_filled"],
                    f["fees_usd"] or 0, cur_bid, cur_ask,
                )
                if pnl_exec is not None:
                    unrealized_exec += pnl_exec

            if f["side"] == "buy":
                entry_capital += f["price_filled"] * f["size_filled"]
            fees_paid += f["fees_usd"] or 0

            # Latest entry per leg (for display)
            if f["leg"] == "kalshi":
                k_entry_price = f["price_filled"]
                k_entry_side = f["side"]
            elif f["leg"] == "poly_global":
                p_entry_price = f["price_filled"]
                p_entry_side = f["side"]

        days_open = (now_ts - earliest_ts) / 86400.0
        # Resolution time: prefer Polymarket close (cleaner), fall back to Kalshi
        close_time = ap["p_close"] or ap["k_close"]
        days_to_resolve = (close_time - now_ts) / 86400.0 if close_time else None

        title = (ap["k_title"] or ap["p_title"] or pair_id)[:80]
        direction = fills[0]["direction"] if fills[0]["direction"] else "?"

        open_positions.append({
            "pair_id": pair_id,
            "kalshi_ticker": ap["kalshi_ticker"],
            "title": title,
            "n_legs": len(fills),
            "entry_capital_usd": round(entry_capital, 2),
            "fees_paid_usd": round(fees_paid, 4),
            "direction": direction,
            "k_entry_price": k_entry_price,
            "k_entry_side": k_entry_side,
            "p_entry_price": p_entry_price,
            "p_entry_side": p_entry_side,
            "k_mid": round(k_mid, 4) if k_mid is not None else None,
            "p_mid": round(p_mid, 4) if p_mid is not None else None,
            "unrealized_mid": round(unrealized_mid, 2),
            "unrealized_executable": round(unrealized_exec, 2),
            "unrealized_mid_pct": (unrealized_mid / entry_capital * 100.0)
                                  if entry_capital > 0 else None,
            "days_open": round(days_open, 2),
            "days_to_resolve": round(days_to_resolve, 1) if days_to_resolve is not None else None,
        })
        total_unrealized_mid += unrealized_mid
        total_unrealized_exec += unrealized_exec
        total_open_capital += entry_capital

    # Sort: worst losers at top (so user sees risk first)
    open_positions.sort(key=lambda x: x["unrealized_mid"])

    # ── 3. Recent settled fills (last 30) ──────────────────────────────
    settled_rows = conn.execute(
        f"""
        SELECT pf.pair_id, pf.leg, pf.side, pf.price_filled, pf.size_filled,
               pf.realized_outcome, pf.realized_pnl_usd, pf.settled_ts,
               pf.settle_method,
               COALESCE(km.title, pm.title) AS title
          FROM paper_fills pf
          LEFT JOIN approved_pairs ap ON ap.pair_id = pf.pair_id
          LEFT JOIN markets km ON km.venue='kalshi'      AND km.venue_market_id=ap.kalshi_ticker
          LEFT JOIN markets pm ON pm.venue='poly_global' AND pm.venue_market_id=ap.poly_global_market_id
         WHERE pf.realized_outcome IS NOT NULL
           AND pf.realized_outcome NOT IN ({",".join("?" * len(WIPE_TAGS))})
        ORDER BY pf.settled_ts DESC
        LIMIT 30
        """,
        WIPE_TAGS,
    ).fetchall()
    settled_positions = [dict(r) for r in settled_rows]

    # ── 4. Daily realized PnL series (last 30 days) ────────────────────
    daily_rows = conn.execute(
        f"""
        SELECT date(settled_ts, 'unixepoch') AS day,
               SUM(realized_pnl_usd)         AS pnl,
               COUNT(*)                      AS n_fills
          FROM paper_fills
         WHERE realized_outcome IS NOT NULL
           AND realized_outcome NOT IN ({",".join("?" * len(WIPE_TAGS))})
         GROUP BY day
         ORDER BY day DESC
         LIMIT 30
        """,
        WIPE_TAGS,
    ).fetchall()
    daily_pnl = [dict(r) for r in daily_rows]

    return {
        "as_of_ts": now_ts,
        "metrics": {
            "realized_pnl_total": round(realized_total, 2),
            "realized_pnl_today": round(realized_today, 2),
            "unrealized_pnl_mid": round(total_unrealized_mid, 2),
            "unrealized_pnl_executable": round(total_unrealized_exec, 2),
            "total_pnl_mid": round(realized_total + total_unrealized_mid, 2),
            "open_capital_usd": round(total_open_capital, 2),
            "open_positions_count": len(open_positions),
            "fills_open": fills_status["open_n"] or 0,
            "fills_settled": fills_status["settled_n"] or 0,
            "fills_wiped": fills_status["wiped_n"] or 0,
            "fills_total": fills_status["total_n"] or 0,
        },
        "open_positions": open_positions,
        "settled_positions": settled_positions,
        "daily_pnl": daily_pnl,
    }
