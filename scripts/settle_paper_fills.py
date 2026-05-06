"""Settle paper fills against resolved markets.

Walks paper_fills where realized_outcome IS NULL, looks up each leg's
underlying market, and back-fills realized PnL when the market has
resolved. Two heuristics:

  1. price_extreme: If status='closed' AND last YES price snapped to
     either ~$1 (>=0.97) or ~$0 (<=0.03), infer YES or NO. Conservative
     thresholds catch most resolved markets without false-positives on
     mid-priced markets that have just stopped trading.
  2. venue_status: If we add explicit Kalshi 'result' field or Poly
     resolution oracle later, this is the upgrade path. Currently only
     price_extreme is implemented.

PnL math per leg (size_filled = N units, price_filled = P, fees = F):
  BUY YES,  outcome YES: pnl = N * (1 - P) - F     (each unit pays $1)
  BUY YES,  outcome NO : pnl = -N * P - F          (worthless)
  SELL YES, outcome YES: pnl = N * (P - 1) - F     (you owe $1 per unit)
  SELL YES, outcome NO : pnl = N * P - F           (kept the premium)

Idempotent: skips fills that already have realized_outcome set.

Usage:
    .venv/bin/python scripts/settle_paper_fills.py
    .venv/bin/python scripts/settle_paper_fills.py --dry-run
    .venv/bin/python scripts/settle_paper_fills.py --price-low 0.05 --price-high 0.95
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))

# Default heuristic thresholds (override via CLI flags). Conservative:
# require the market to be CLOSED + the YES price to be at an extreme.
PRICE_HIGH = 0.97   # >= → infer YES
PRICE_LOW = 0.03    # <= → infer NO


def _resolve_outcome(yes_bid: float | None, yes_ask: float | None,
                     status: str | None,
                     price_high: float, price_low: float) -> str | None:
    """Heuristic: returns 'yes', 'no', or None (still pending)."""
    if (status or "").lower() not in ("closed", "resolved", "finalized", "settled"):
        return None
    # Use mid if both present, otherwise whichever is set
    prices = [p for p in (yes_bid, yes_ask) if p is not None]
    if not prices:
        return None
    mid = sum(prices) / len(prices)
    if mid >= price_high:
        return "yes"
    if mid <= price_low:
        return "no"
    # Mid-range price on a closed market — ambiguous, leave pending
    return None


def _pnl_for_leg(side: str, price_filled: float | None,
                 size_filled: int | None, fees_usd: float | None,
                 outcome: str) -> float:
    """Net realized PnL for a single leg given the market outcome."""
    p = price_filled or 0.0
    n = size_filled or 0
    f = fees_usd or 0.0
    if side == "buy":
        # Pay p × n upfront; receive $1 per unit if outcome=YES, else $0
        return (n * (1.0 - p) - f) if outcome == "yes" else (-n * p - f)
    else:  # sell (short)
        # Receive p × n upfront; owe $1 per unit if outcome=YES, else $0
        return (n * (p - 1.0) - f) if outcome == "yes" else (n * p - f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be settled, don't write")
    parser.add_argument("--price-high", type=float, default=PRICE_HIGH,
                        help="YES mid >= this -> infer YES outcome (default 0.97)")
    parser.add_argument("--price-low", type=float, default=PRICE_LOW,
                        help="YES mid <= this -> infer NO outcome (default 0.03)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema, transaction

    cfg = load_config()
    init_schema(cfg.db_path)

    print(f"=== settle_paper_fills ({'DRY RUN' if args.dry_run else 'COMMIT'}) ===")
    print(f"  thresholds: yes_mid >= {args.price_high} → YES, "
          f"<= {args.price_low} → NO")
    print()

    with connect(cfg.db_path) as conn:
        # Pending = no realized_outcome yet, fill is in 'filled' state
        pending = conn.execute("""
            SELECT pf.id, pf.signal_id, pf.pair_id, pf.leg, pf.side,
                   pf.price_filled, pf.size_filled, pf.fees_usd,
                   ap.kalshi_ticker, ap.poly_global_market_id
            FROM paper_fills pf
            LEFT JOIN approved_pairs ap ON ap.pair_id = pf.pair_id
            WHERE pf.realized_outcome IS NULL
              AND pf.state = 'filled'
        """).fetchall()
        print(f"  pending fills            : {len(pending)}")

        if not pending:
            print("  nothing to settle.")
            return 0

        n_resolved = 0
        n_still_pending = 0
        n_no_market = 0
        outcome_counts = {"yes": 0, "no": 0}
        rows_to_update: list[tuple] = []

        for f in pending:
            # Determine which venue's market to look up based on leg name
            if f["leg"] == "kalshi":
                m = conn.execute(
                    "SELECT yes_bid, yes_ask, status FROM markets "
                    "WHERE venue='kalshi' AND venue_market_id=?",
                    (f["kalshi_ticker"],),
                ).fetchone()
            else:  # poly_global
                m = conn.execute(
                    "SELECT yes_bid, yes_ask, status FROM markets "
                    "WHERE venue='poly_global' AND venue_market_id=?",
                    (f["poly_global_market_id"],),
                ).fetchone()
            if not m:
                n_no_market += 1
                continue
            outcome = _resolve_outcome(
                m["yes_bid"], m["yes_ask"], m["status"],
                args.price_high, args.price_low,
            )
            if outcome is None:
                n_still_pending += 1
                continue
            pnl = _pnl_for_leg(
                f["side"], f["price_filled"], f["size_filled"], f["fees_usd"],
                outcome,
            )
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            rows_to_update.append((
                outcome, pnl, int(time.time()), "price_extreme", f["id"],
            ))
            n_resolved += 1

        print(f"  resolved this run        : {n_resolved}")
        print(f"    YES outcomes           : {outcome_counts.get('yes', 0)}")
        print(f"    NO  outcomes           : {outcome_counts.get('no', 0)}")
        print(f"  still pending            : {n_still_pending}")
        print(f"  no underlying market     : {n_no_market}")
        print()

        if args.dry_run:
            print("DRY RUN — no rows written.")
            return 0

        if rows_to_update:
            with transaction(conn):
                conn.executemany(
                    """
                    UPDATE paper_fills
                    SET realized_outcome = ?,
                        realized_pnl_usd = ?,
                        settled_ts = ?,
                        settle_method = ?
                    WHERE id = ?
                    """,
                    rows_to_update,
                )

        # Realized PnL summary
        agg = conn.execute("""
            SELECT
                COUNT(*) AS n_fills,
                SUM(CASE WHEN realized_outcome IS NULL THEN 1 ELSE 0 END) AS open_fills,
                SUM(realized_pnl_usd) AS total_pnl,
                SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS win_fills,
                SUM(CASE WHEN realized_pnl_usd < 0 THEN 1 ELSE 0 END) AS loss_fills
            FROM paper_fills WHERE state='filled'
        """).fetchone()
        print(f"=== Cumulative paper PnL (after settlement) ===")
        print(f"  total fills              : {agg['n_fills']}")
        print(f"  open                     : {agg['open_fills']}")
        print(f"  settled wins             : {agg['win_fills']}")
        print(f"  settled losses           : {agg['loss_fills']}")
        if agg['total_pnl'] is not None:
            print(f"  net realized PnL         : ${agg['total_pnl']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
