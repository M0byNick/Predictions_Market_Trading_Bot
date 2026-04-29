"""Submit pending candidate pairs to the Anthropic Message Batches API.

Usage:
    .venv/bin/python scripts/submit_batch.py            # submit all pending
    .venv/bin/python scripts/submit_batch.py --max 10   # cap (for testing)
    .venv/bin/python scripts/submit_batch.py --dry-run  # don't submit, just count

Saves the batch_id to data/batch_state.json so poll_batch.py can pick it up
in the morning.

Cost on Sonnet 4.5 batch (50% discount):
    ~600 input + ~175 output tokens per pair
    18,854 pairs ~= $42 total
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None, help="cap candidates (test runs)")
    parser.add_argument("--dry-run", action="store_true", help="count only; don't submit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    load_dotenv(_ARB_ROOT / ".env", override=True)

    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from anthropic import Anthropic

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema
    from arb_bot.mapping.adjudicator import SYSTEM_PROMPT, _render_prompt
    from arb_bot.mapping.embeddings import pending_candidates

    cfg = load_config()
    init_schema(cfg.db_path)

    print("== Submit batch ==")
    print(f"  model     : {cfg.anthropic_model}")
    print(f"  db        : {cfg.db_path}")
    print()

    with connect(cfg.db_path) as conn:
        candidates = list(pending_candidates(conn))
        if args.max:
            candidates = candidates[: args.max]
        n = len(candidates)
        print(f"  pending candidates: {n}")
        if n == 0:
            print("  nothing to submit.")
            return 0

        # Build the batch request payload.
        print(f"  rendering {n} prompts...")
        t0 = time.time()
        requests = []
        skipped = 0
        for cand in candidates:
            try:
                prompt = _render_prompt(cand, conn)
            except Exception as e:
                logging.warning("Skip candidate %d: %s", cand["id"], e)
                skipped += 1
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
        print(f"  rendered {len(requests)} prompts in {time.time()-t0:.1f}s "
              f"(skipped {skipped})")

        # Crude size check
        approx_input = sum(len(r["params"]["messages"][0]["content"]) for r in requests)
        print(f"  approx total prompt chars: {approx_input:,}")
        print(f"  approx total prompt tokens: ~{approx_input//4:,} (≈ chars/4)")

    if args.dry_run:
        print("\nDRY RUN — not submitting.")
        return 0

    print()
    print("Submitting to Anthropic Message Batches API...")
    client = Anthropic(api_key=cfg.anthropic_api_key)
    try:
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1

    print(f"OK   : batch submitted")
    print(f"  batch_id        : {batch.id}")
    print(f"  status          : {batch.processing_status}")
    print(f"  request_counts  : {batch.request_counts}")
    print(f"  expires_at      : {batch.expires_at}")
    print()

    # Persist state for poll_batch.py
    state_path = cfg.data_dir / "batch_state.json"
    state = {
        "batch_id": batch.id,
        "model": cfg.anthropic_model,
        "submitted_ts": int(time.time()),
        "n_requests": len(requests),
        "expires_at": str(batch.expires_at),
    }
    state_path.write_text(json.dumps(state, indent=2))
    print(f"  state saved to  : {state_path}")
    print()
    print("To check status / collect results:")
    print(f"  .venv/bin/python scripts/poll_batch.py")
    print()
    print("Anthropic typically completes batches in 1-4h for jobs this size; max 24h.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
