"""Run Opus 4.7 over the LLM's most-uncertain tiers — pattern-discovery audit.

Scope (deduplicated union):
  1. Review-recommended  : match=yes, risk in (none,low), has edge_case_flags
  2. Ambiguous           : match=ambiguous
  3. Auto-flagged HIGH   : edge_case_downgraded=1
  4. Inverse polarity    : match=yes AND match_polarity=inverse

Verdicts are stored alongside Sonnet's via the (candidate_id, model)
de-duplication in pair_verdicts. Compare with scripts/compare_models.py
once the batch ends.

Usage:
    .venv/bin/python scripts/submit_opus_audit.py --dry-run
    .venv/bin/python scripts/submit_opus_audit.py
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


# Opus 4.7 batch pricing (50% off standard) circa 2026:
#   input  ~$7.50 / M tokens
#   output ~$37.50 / M tokens
OPUS_INPUT_PER_M = 7.50
OPUS_OUTPUT_PER_M = 37.50


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--model", type=str, default="claude-opus-4-7",
                        help="override (default: claude-opus-4-7)")
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

    print("== Opus pattern-discovery audit on suspect tiers ==")
    print(f"  model: {args.model}  (overrides cfg.anthropic_model={cfg.anthropic_model})")
    print()

    with connect(cfg.db_path) as conn:
        # Union of the 4 categories. Use a CTE that takes the latest verdict
        # per candidate. Skip pairs that have already received an Opus verdict.
        rows = conn.execute("""
            WITH latest AS (
                SELECT v.*
                FROM pair_verdicts v
                JOIN (
                    SELECT candidate_id, MAX(verdict_ts) AS ts
                    FROM pair_verdicts
                    GROUP BY candidate_id
                ) t ON t.candidate_id = v.candidate_id AND t.ts = v.verdict_ts
            ),
            existing_opus AS (
                SELECT candidate_id FROM pair_verdicts
                WHERE model LIKE 'claude-opus%'
            )
            SELECT c.id, c.kalshi_ticker, c.poly_global_market_id, c.cosine_similarity,
                   l.match, l.match_polarity, l.resolution_divergence_risk,
                   l.edge_case_downgraded, l.edge_case_flags
            FROM candidate_pairs c
            JOIN latest l ON l.candidate_id = c.id
            LEFT JOIN approved_pairs a
              ON a.kalshi_ticker = c.kalshi_ticker AND a.poly_global_market_id = c.poly_global_market_id
            LEFT JOIN rejected_pairs r ON r.candidate_id = c.id
            LEFT JOIN existing_opus e ON e.candidate_id = c.id
            WHERE a.pair_id IS NULL
              AND r.candidate_id IS NULL
              AND e.candidate_id IS NULL
              AND (
                -- review-recommended
                (l.match='yes' AND l.resolution_divergence_risk IN ('none','low')
                  AND l.edge_case_flags IS NOT NULL AND l.edge_case_flags != '[]')
                -- ambiguous
                OR l.match='ambiguous'
                -- auto-flagged HIGH
                OR l.edge_case_downgraded = 1
                -- inverse polarity
                OR (l.match='yes' AND l.match_polarity='inverse')
              )
        """).fetchall()
        if args.max:
            rows = rows[: args.max]

        n = len(rows)
        print(f"  candidates in audit scope: {n}")
        print()

        # Breakdown
        n_amb = sum(1 for r in rows if r["match"] == "ambiguous")
        n_yes_flagged = sum(1 for r in rows
                            if r["match"] == "yes"
                            and r["resolution_divergence_risk"] in ("none", "low")
                            and r["edge_case_flags"] not in (None, "[]"))
        n_dgrd = sum(1 for r in rows if r["edge_case_downgraded"])
        n_inv = sum(1 for r in rows if r["match"] == "yes" and r["match_polarity"] == "inverse")
        print("  breakdown (categories may overlap):")
        print(f"    ambiguous              : {n_amb}")
        print(f"    review-recommended     : {n_yes_flagged}")
        print(f"    auto-flagged HIGH      : {n_dgrd}")
        print(f"    inverse polarity tag   : {n_inv}")
        print()

        # Cost estimate
        est_in_dollars = n * 600 / 1_000_000 * OPUS_INPUT_PER_M
        est_out_dollars = n * 175 / 1_000_000 * OPUS_OUTPUT_PER_M
        total = est_in_dollars + est_out_dollars
        print(f"  estimated batch cost: ~${total:.2f}")
        print(f"    ({n} pairs × Opus 4.7 batch rates × 50% discount)")
        print()

        if args.dry_run:
            print("DRY RUN — not submitting.")
            return 0

        # Render prompts
        print(f"  rendering {n} prompts...")
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
                        "model": args.model,
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
    print("Submitting Opus batch...")
    client = Anthropic(api_key=cfg.anthropic_api_key)
    try:
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1

    print(f"OK   : batch {batch.id}")
    print(f"  status         : {batch.processing_status}")
    print(f"  request_counts : {batch.request_counts}")

    state_path = cfg.data_dir / "batch_state_opus.json"
    state_path.write_text(json.dumps({
        "batch_id": batch.id,
        "model": args.model,
        "submitted_ts": int(time.time()),
        "n_requests": len(requests),
        "purpose": "Opus pattern-discovery audit on suspect tiers",
    }, indent=2))
    print(f"  state saved to : {state_path}")
    print()
    print("To monitor / collect:")
    print(f"  .venv/bin/python scripts/poll_batch.py {batch.id}")
    print()
    print("To compare Opus vs Sonnet verdicts once collected:")
    print(f"  .venv/bin/python scripts/compare_models.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
