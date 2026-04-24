"""One-shot seed job.

Usage:
    python -u scripts/seed_candidates.py            # full pipeline
    python -u scripts/seed_candidates.py --sync     # adjudicate synchronously (dev)
    python -u scripts/seed_candidates.py --collect <batch_id>
"""
from __future__ import annotations

import argparse
import logging
import sys

from arb_bot.config import load_config
from arb_bot.db import connect, init_schema
from arb_bot.ingest import kalshi as kalshi_ingest
from arb_bot.ingest import polymarket_us as poly_ingest
from arb_bot.mapping import adjudicator, embeddings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true", help="adjudicate synchronously (dev)")
    parser.add_argument("--max", type=int, default=None, help="cap sync adjudications")
    parser.add_argument("--collect", type=str, default=None, help="collect batch_id results")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-embed", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    init_schema(cfg.db_path)

    with connect(cfg.db_path) as conn:
        if args.collect:
            adjudicator.collect_batch_results(conn, cfg, args.collect)
            return 0

        if not args.skip_ingest:
            kalshi_ingest.upsert_markets(conn, cfg)
            poly_ingest.upsert_markets(conn, cfg)
        if not args.skip_embed:
            embeddings.compute_missing(conn, cfg)
            embeddings.generate_candidates(conn, cfg)

        pending = list(embeddings.pending_candidates(conn))
        print(f"Pending candidates awaiting adjudication: {len(pending)}")
        if not pending:
            return 0

        if args.sync:
            adjudicator.adjudicate_sync(conn, cfg, pending, max_items=args.max)
        else:
            batch_id = adjudicator.adjudicate_batch(conn, cfg, pending)
            print(f"Submitted batch: {batch_id}")
            print("Run again with --collect <batch_id> once the batch reports 'ended' (within 24h).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
