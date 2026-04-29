"""Check batch status and (when ready) write verdicts to the DB.

Usage:
    .venv/bin/python scripts/poll_batch.py                  # use saved batch_id
    .venv/bin/python scripts/poll_batch.py <batch_id>       # explicit
    .venv/bin/python scripts/poll_batch.py --watch          # poll every 60s until done
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


def _load_batch_id(state_path: Path) -> str | None:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())["batch_id"]
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch_id", nargs="?", default=None)
    parser.add_argument("--watch", action="store_true", help="poll every 60s until ended")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from anthropic import Anthropic

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema
    from arb_bot.mapping.adjudicator import collect_batch_results

    cfg = load_config()
    init_schema(cfg.db_path)

    batch_id = args.batch_id or _load_batch_id(cfg.data_dir / "batch_state.json")
    if not batch_id:
        print("FAIL: no batch_id (pass as arg or run submit_batch.py first)")
        return 1

    client = Anthropic(api_key=cfg.anthropic_api_key)

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        cnts = batch.request_counts
        print(f"== batch {batch_id} ==")
        print(f"  status          : {batch.processing_status}")
        print(f"  processing      : {cnts.processing}")
        print(f"  succeeded       : {cnts.succeeded}")
        print(f"  errored         : {cnts.errored}")
        print(f"  canceled        : {cnts.canceled}")
        print(f"  expired         : {cnts.expired}")
        print(f"  ended_at        : {batch.ended_at or '<not yet>'}")

        if batch.processing_status == "ended":
            print()
            print("Batch ended. Collecting results to DB...")
            with connect(cfg.db_path) as conn:
                n = collect_batch_results(conn, cfg, batch_id)
            print(f"OK   : wrote {n} verdicts to pair_verdicts table")
            print()
            print("Next: open the dashboard to start approving:")
            print("  .venv/bin/python -m arb_bot.dashboard.app")
            print("  → http://127.0.0.1:8090")
            return 0

        if not args.watch:
            print()
            print("Not done yet. Re-run later, or use --watch to wait.")
            return 0

        print("  ...not done; sleeping 60s")
        print()
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
