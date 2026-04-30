"""Daily pipeline: ingest → embed → generate candidates → submit batch.

Designed to run from cron. Idempotent — safe to run multiple times per day.

Behavior:
  1. Refresh markets from both venues (Kalshi + Polymarket Global).
  2. Compute embeddings for any newly-ingested markets (local, free).
  3. Generate top-K candidate pairs via cosine similarity (local, free).
  4. Find pending candidates (no verdict yet).
  5. If pending > MIN_BATCH_SIZE, submit to Anthropic Message Batches API.
  6. Save batch_id to data/batch_state_cron_YYYYMMDD.json for the polling job.
  7. Logs go to data/logs/daily_pipeline_YYYYMMDD.log.

Exit codes:
  0  = success (or nothing-to-do)
  1  = ingest failure
  2  = embedding/candidate failure
  3  = batch submission failure
"""
from __future__ import annotations

import argparse
import json
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

# Don't submit a batch for fewer than this many new pairs (saves on
# Anthropic minimum-batch overhead and keeps logs clean).
MIN_BATCH_SIZE = 10


def _setup_logging(log_dir: Path, day: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    log_path = log_dir / f"daily_pipeline_{day}.log"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )
    return logging.getLogger("daily_pipeline")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-ingest", action="store_true",
                        help="dev: skip Kalshi/Poly market refresh")
    parser.add_argument("--skip-batch", action="store_true",
                        help="dev: stop after candidate generation, don't submit")
    parser.add_argument("--max", type=int, default=None,
                        help="cap batch size (test runs)")
    args = parser.parse_args()

    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema
    from arb_bot.heartbeat import touch
    from arb_bot.ingest import kalshi as kalshi_ingest
    from arb_bot.ingest import polymarket_global as poly_ingest
    from arb_bot.mapping import embeddings as emb
    from arb_bot.mapping.adjudicator import SYSTEM_PROMPT, _render_prompt
    from arb_bot.mapping.embeddings import pending_candidates

    cfg = load_config()
    init_schema(cfg.db_path)

    day = time.strftime("%Y%m%d", time.gmtime())
    log = _setup_logging(cfg.data_dir / "logs", day)
    log.info("=== run_daily_pipeline START (%s) ===", day)

    # ──────────────────────────────────────────────────────────────────
    # Step 1: ingest
    # ──────────────────────────────────────────────────────────────────
    if not args.skip_ingest:
        with connect(cfg.db_path) as conn:
            try:
                n_kal = kalshi_ingest.upsert_markets(conn, cfg)
                log.info("kalshi: upserted %d markets", n_kal)
            except Exception:
                log.exception("kalshi ingest failed")
                return 1
            try:
                n_pol = poly_ingest.upsert_markets(conn, cfg)
                log.info("poly_global: upserted %d markets", n_pol)
            except Exception:
                log.exception("poly_global ingest failed")
                return 1
        touch(cfg.heartbeat_path)
    else:
        log.info("skip-ingest set; reusing existing markets")

    # ──────────────────────────────────────────────────────────────────
    # Step 2-3: embed new markets + generate candidate pairs
    # ──────────────────────────────────────────────────────────────────
    with connect(cfg.db_path) as conn:
        try:
            n_emb = emb.compute_missing(conn, cfg)
            log.info("embeddings: computed %d new", n_emb)
            n_pairs = emb.generate_candidates(conn, cfg)
            log.info("candidate pairs: %d new", n_pairs)
        except Exception:
            log.exception("embedding / candidate gen failed")
            return 2

    if args.skip_batch:
        log.info("skip-batch set; stopping before submission")
        return 0

    # ──────────────────────────────────────────────────────────────────
    # Step 4-6: submit pending candidates as Anthropic batch
    # ──────────────────────────────────────────────────────────────────
    with connect(cfg.db_path) as conn:
        candidates = list(pending_candidates(conn))
    if args.max:
        candidates = candidates[: args.max]
    log.info("pending candidates: %d", len(candidates))

    if len(candidates) < MIN_BATCH_SIZE:
        log.info("fewer than MIN_BATCH_SIZE=%d pending; skipping batch", MIN_BATCH_SIZE)
        log.info("=== run_daily_pipeline END (no batch) ===")
        return 0

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=cfg.anthropic_api_key)

        with connect(cfg.db_path) as conn:
            requests = []
            for cand in candidates:
                try:
                    prompt = _render_prompt(cand, conn)
                except Exception as e:
                    log.warning("skip candidate %d: %s", cand["id"], e)
                    continue
                requests.append(
                    {
                        "custom_id": f"cand-{cand['id']}",
                        "params": {
                            "model": cfg.anthropic_model,
                            "max_tokens": 1024,
                            "system": SYSTEM_PROMPT,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    }
                )
        log.info("rendered %d prompts", len(requests))

        batch = client.messages.batches.create(requests=requests)
        log.info("submitted batch %s (status=%s)", batch.id, batch.processing_status)

        # Persist for run_collection.py
        state_path = cfg.data_dir / f"batch_state_cron_{day}.json"
        state_path.write_text(json.dumps({
            "batch_id": batch.id,
            "model": cfg.anthropic_model,
            "submitted_ts": int(time.time()),
            "n_requests": len(requests),
            "purpose": "daily cron",
        }, indent=2))
        log.info("state saved to %s", state_path)
    except Exception:
        log.exception("batch submission failed")
        return 3

    log.info("=== run_daily_pipeline END (batch %s submitted) ===", batch.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
