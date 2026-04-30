"""Bulk-approve the safest tier as the initial tradeable universe.

Selects pairs whose LATEST verdict is:
  - match=yes
  - resolution_divergence_risk in (none, low)
  - no edge_case_flags

…and that are not already approved/rejected. Inserts them into
approved_pairs with tag='clean' and polarity copied from the latest
verdict. Inverse-polarity pairs are still marked clean (the bot's
signal/spread.py uses polarity to pick the right arb math; it doesn't
need to be hand-tagged high_risk).

Usage:
    .venv/bin/python scripts/bulk_approve_safest.py --dry-run
    .venv/bin/python scripts/bulk_approve_safest.py
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approver", type=str, default="bulk-script")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema, transaction

    cfg = load_config()
    init_schema(cfg.db_path)

    with connect(cfg.db_path) as conn:
        # Latest verdict per candidate that meets the safest criteria,
        # excluding already-approved/rejected pairs.
        rows = conn.execute("""
            WITH latest AS (
                SELECT v.*
                FROM pair_verdicts v
                JOIN (
                    SELECT candidate_id, MAX(verdict_ts) AS ts, MAX(id) AS rid
                    FROM pair_verdicts GROUP BY candidate_id
                ) t ON t.candidate_id = v.candidate_id
                   AND t.ts = v.verdict_ts
            )
            SELECT c.id AS candidate_id,
                   c.kalshi_ticker,
                   c.poly_global_market_id,
                   l.normalized_question,
                   l.resolution_divergence_risk,
                   l.match_polarity,
                   l.confidence,
                   l.model
            FROM candidate_pairs c
            JOIN latest l ON l.candidate_id = c.id
            LEFT JOIN approved_pairs a
              ON a.kalshi_ticker = c.kalshi_ticker
             AND a.poly_global_market_id = c.poly_global_market_id
            LEFT JOIN rejected_pairs r ON r.candidate_id = c.id
            WHERE a.pair_id IS NULL AND r.candidate_id IS NULL
              AND l.match = 'yes'
              AND l.resolution_divergence_risk IN ('none', 'low')
              AND (l.edge_case_flags IS NULL OR l.edge_case_flags = '[]')
        """).fetchall()

        n = len(rows)
        print(f"== Bulk approve safest ==")
        print(f"  pairs to approve: {n}")
        if n == 0:
            print("  nothing to do.")
            return 0

        # Distribution
        from collections import Counter
        pol = Counter(r["match_polarity"] for r in rows)
        risk = Counter(r["resolution_divergence_risk"] for r in rows)
        model = Counter(r["model"] for r in rows)
        print(f"  by polarity : {dict(pol)}")
        print(f"  by risk     : {dict(risk)}")
        print(f"  by model    : {dict(model)}")
        print()

        if args.dry_run:
            print("DRY RUN — not writing.")
            return 0

        now_ts = int(time.time())
        with transaction(conn):
            for r in rows:
                pair_id = f"{r['kalshi_ticker']}__{r['poly_global_market_id']}"[:120]
                pol_v = r["match_polarity"] or "unknown"
                if pol_v not in ("same", "inverse", "unknown"):
                    pol_v = "unknown"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO approved_pairs
                    (pair_id, kalshi_ticker, poly_global_market_id, normalized_question,
                     resolution_divergence_risk, match_polarity, tag, approved_by,
                     approved_ts, active, notes)
                    VALUES (?, ?, ?, ?, ?, ?, 'clean', ?, ?, 1, ?)
                    """,
                    (
                        pair_id,
                        r["kalshi_ticker"],
                        r["poly_global_market_id"],
                        r["normalized_question"],
                        r["resolution_divergence_risk"],
                        pol_v,
                        args.approver,
                        now_ts,
                        f"bulk-approved from safest tier; model={r['model']}",
                    ),
                )
        print(f"OK   : approved {n} pairs as clean")
        print()
        print("Verify with the dashboard:")
        print("  http://127.0.0.1:8090/approved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
