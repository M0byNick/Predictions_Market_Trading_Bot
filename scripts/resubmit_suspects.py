"""Re-submit a focused subset to Anthropic batch with the new prompt
(now asks for match_polarity + has improved edge-case warnings).

Targets:
  * pair_verdicts.match='ambiguous' AND polarity='unknown'   (Bug B from review)
  * pair_verdicts.match='yes' AND match_polarity='unknown'   (need polarity call)

Usage:
    .venv/bin/python scripts/resubmit_suspects.py --dry-run
    .venv/bin/python scripts/resubmit_suspects.py
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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from anthropic import Anthropic

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema
    from arb_bot.mapping.adjudicator import SYSTEM_PROMPT, _render_prompt

    cfg = load_config()
    init_schema(cfg.db_path)

    print("== Resubmit suspect pairs (ambiguous + match=yes/polarity=unknown) ==")
    print(f"  model: {cfg.anthropic_model}")
    print()

    with connect(cfg.db_path) as conn:
        # Find candidate pairs whose latest verdict is in the suspect set,
        # AND that are still pending human review (not approved/rejected)
        rows = conn.execute("""
            SELECT c.id, c.kalshi_ticker, c.poly_global_market_id, c.cosine_similarity,
                   v.match, v.match_polarity, v.resolution_divergence_risk
            FROM candidate_pairs c
            JOIN pair_verdicts v ON v.id = (
                SELECT id FROM pair_verdicts WHERE candidate_id = c.id
                ORDER BY verdict_ts DESC LIMIT 1
            )
            LEFT JOIN approved_pairs a
              ON a.kalshi_ticker = c.kalshi_ticker AND a.poly_global_market_id = c.poly_global_market_id
            LEFT JOIN rejected_pairs r ON r.candidate_id = c.id
            WHERE a.pair_id IS NULL AND r.candidate_id IS NULL
              AND (
                v.match = 'ambiguous'
                OR (v.match = 'yes' AND v.match_polarity = 'unknown')
              )
        """).fetchall()
        if args.max:
            rows = rows[: args.max]

        n = len(rows)
        print(f"  candidates to re-adjudicate: {n}")
        print()
        print("  breakdown:")
        amb = sum(1 for r in rows if r["match"] == "ambiguous")
        yes_unk = sum(1 for r in rows if r["match"] == "yes")
        print(f"    ambiguous              : {amb}")
        print(f"    match=yes, polarity=?  : {yes_unk}")
        print()

        # Estimate cost: ~600 input + 175 output per pair, Sonnet batch rates
        est_in = n * 600 / 1_000_000 * 1.50
        est_out = n * 175 / 1_000_000 * 7.50
        print(f"  estimated cost: ~${est_in + est_out:.2f}")
        print()

        if args.dry_run:
            print("DRY RUN — not submitting.")
            return 0

        # Render prompts
        print("  rendering prompts...")
        requests = []
        skipped = 0
        for cand in rows:
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
        print(f"  rendered {len(requests)} (skipped {skipped})")

        if not requests:
            print("  nothing to submit.")
            return 0

    print()
    print("Submitting batch...")
    client = Anthropic(api_key=cfg.anthropic_api_key)
    try:
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1

    print(f"OK   : batch {batch.id}")
    print(f"  status         : {batch.processing_status}")
    print(f"  request_counts : {batch.request_counts}")

    # Save batch_id alongside the original (don't clobber)
    state_path = cfg.data_dir / "batch_state_resubmit.json"
    state_path.write_text(json.dumps({
        "batch_id": batch.id,
        "model": cfg.anthropic_model,
        "submitted_ts": int(time.time()),
        "n_requests": len(requests),
        "purpose": "ambiguous + polarity-unknown re-adjudication",
    }, indent=2))
    print(f"  state saved to : {state_path}")
    print()
    print("Collect later with:")
    print(f"  .venv/bin/python scripts/poll_batch.py {batch.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
