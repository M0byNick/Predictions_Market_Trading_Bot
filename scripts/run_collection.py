"""Collection job: poll any in-flight batches and collect when ready.

Designed to run from cron every 30 minutes from ~04:00-12:00 UTC after the
daily pipeline submits at 03:00 UTC. Idempotent — safe to run any time.

For each `data/batch_state_cron_*.json` file:
  * Skip if already collected (state file gets renamed with .done suffix).
  * Else: query Anthropic for batch status.
  * If status='ended', collect verdicts, run edge_cases.flag_all_verdicts,
    run backfill_polarity, rename state file to .done.
  * Else: leave for next run.
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


def _setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y%m%d", time.gmtime())
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_dir / f"collection_{day}.log"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("collection")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-glob", default="batch_state_cron_*.json",
                        help="filename pattern for state files in data/")
    args = parser.parse_args()

    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from anthropic import Anthropic

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema
    from arb_bot.heartbeat import touch
    from arb_bot.mapping.adjudicator import collect_batch_results
    from arb_bot.mapping.edge_cases import backfill_polarity, flag_all_verdicts

    cfg = load_config()
    init_schema(cfg.db_path)
    log = _setup_logging(cfg.data_dir / "logs")

    state_files = sorted(cfg.data_dir.glob(args.state_glob))
    if not state_files:
        log.info("no in-flight batch state files found")
        return 0

    client = Anthropic(api_key=cfg.anthropic_api_key)

    for sf in state_files:
        try:
            state = json.loads(sf.read_text())
        except Exception as e:
            log.warning("could not read %s: %s", sf, e)
            continue

        batch_id = state.get("batch_id")
        if not batch_id:
            log.warning("no batch_id in %s; skipping", sf)
            continue

        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception:
            log.exception("retrieve(%s) failed", batch_id)
            continue

        log.info(
            "batch %s status=%s succeeded=%d errored=%d",
            batch_id, batch.processing_status,
            batch.request_counts.succeeded, batch.request_counts.errored,
        )

        if batch.processing_status != "ended":
            continue

        # Collect, then run flag/polarity passes so the dashboard reflects
        # the new verdicts immediately.
        with connect(cfg.db_path) as conn:
            try:
                n = collect_batch_results(conn, cfg, batch_id)
                log.info("collected %d verdicts from %s", n, batch_id)
            except Exception:
                log.exception("collect_batch_results(%s) failed", batch_id)
                continue
            try:
                n_flagged, n_dgrd, n_total = flag_all_verdicts(conn)
                log.info(
                    "edge-case flagger: flagged=%d/%d, downgraded=%d",
                    n_flagged, n_total, n_dgrd,
                )
            except Exception:
                log.exception("flag_all_verdicts failed")
            try:
                n_inv, n_same = backfill_polarity(conn)
                log.info(
                    "polarity backfill: inverse=%d same=%d",
                    n_inv, n_same,
                )
            except Exception:
                log.exception("backfill_polarity failed")

        # Rename state file with .done so we don't process it again
        done = sf.with_suffix(sf.suffix + ".done")
        sf.rename(done)
        log.info("renamed %s -> %s", sf.name, done.name)
        touch(cfg.heartbeat_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
