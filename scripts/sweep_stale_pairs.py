"""Sweep approved_pairs that are confirmed stale and mark them inactive.

A pair is "confirmed stale" if BOTH legs (kalshi + polymarket) have
markets.last_seen_ts older than --threshold-hours (default 72h). 72h
is well past any plausible temporary outage and indicates the underlying
market is no longer trading on at least one venue.

Two action paths:

  --dry-run  (default): list candidates only, no writes
  --apply           : mark pairs inactive with a tagged note +
                      timestamp so we can audit / reverse later

This complements (does not replace) the 120-min stale-quote guard in
signal generation, which only blocks signals for the cycle. This sweep
removes the pair from the active universe entirely so it stops eating
WS subscription slots, signal-scan cycles, and dashboard real-estate.

Usage:
    python scripts/sweep_stale_pairs.py                # dry run, 72h threshold
    python scripts/sweep_stale_pairs.py --apply        # actually deactivate
    python scripts/sweep_stale_pairs.py --threshold-hours 48 --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold-hours", type=float, default=72.0,
                        help="Hours of staleness on BOTH legs before "
                             "deactivation (default 72)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write deactivations (default: dry run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap number of deactivations (safety bound)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("sweep_stale_pairs")

    load_dotenv(_ARB_ROOT / ".env", override=True)
    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema

    cfg = load_config()
    init_schema(cfg.db_path)

    threshold_sec = int(args.threshold_hours * 3600)
    now_ts = int(time.time())
    cutoff = now_ts - threshold_sec

    with connect(cfg.db_path) as conn:
        # Find active pairs where BOTH legs' last_seen_ts < cutoff.
        # Use LEFT JOIN to also catch pairs where the markets row is
        # missing entirely (the leg has never been seen).
        rows = conn.execute(
            """
            SELECT
                ap.pair_id,
                ap.kalshi_ticker,
                ap.poly_global_market_id,
                COALESCE(km.last_seen_ts, 0)  AS k_seen,
                COALESCE(pm.last_seen_ts, 0)  AS p_seen,
                km.title AS k_title,
                pm.title AS p_title
            FROM approved_pairs ap
            LEFT JOIN markets km
                  ON km.venue='kalshi'      AND km.venue_market_id=ap.kalshi_ticker
            LEFT JOIN markets pm
                  ON pm.venue='poly_global' AND pm.venue_market_id=ap.poly_global_market_id
            WHERE ap.active=1
              AND (km.last_seen_ts IS NULL OR km.last_seen_ts < ?)
              AND (pm.last_seen_ts IS NULL OR pm.last_seen_ts < ?)
            ORDER BY MIN(COALESCE(km.last_seen_ts, 0),
                         COALESCE(pm.last_seen_ts, 0)) ASC
            """,
            (cutoff, cutoff),
        ).fetchall()

        if args.limit:
            rows = rows[: args.limit]

        log.info("threshold=%dh; cutoff_ts=%d; candidates=%d",
                 int(args.threshold_hours), cutoff, len(rows))
        if not rows:
            log.info("no pairs above threshold; nothing to do")
            return 0

        # Print sample
        log.info("sample (top 10 stalest):")
        for r in rows[:10]:
            k_age_h = (now_ts - r["k_seen"]) / 3600 if r["k_seen"] else float("inf")
            p_age_h = (now_ts - r["p_seen"]) / 3600 if r["p_seen"] else float("inf")
            log.info(
                "  %-30s  k_age=%6.1fh  p_age=%6.1fh  %s",
                r["kalshi_ticker"][:30],
                k_age_h, p_age_h,
                ((r["k_title"] or r["p_title"] or ""))[:60],
            )

        if not args.apply:
            log.info("(dry run) re-run with --apply to deactivate")
            return 0

        # Apply: mark inactive + leave a tagged note for auditability.
        # We append to existing notes rather than overwrite.
        n_done = 0
        for r in rows:
            note_addition = (
                f"\n[auto-deactivated by sweep_stale_pairs at {now_ts}: "
                f"both legs stale > {args.threshold_hours}h]"
            )
            conn.execute(
                """
                UPDATE approved_pairs
                   SET active=0,
                       notes=COALESCE(notes,'') || ?
                 WHERE pair_id=?
                """,
                (note_addition, r["pair_id"]),
            )
            n_done += 1
        conn.commit()
        log.info("deactivated %d pairs (active=0, note appended)", n_done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
