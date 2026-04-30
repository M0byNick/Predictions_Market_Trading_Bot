"""Compare verdicts across Sonnet vs Opus on the same pairs.

Surfaces:
  * exact agreement rate on `match`, `match_polarity`, `risk`
  * confusion matrices for the two pivotal fields
  * pairs where the models disagree on `match` (most consequential)
  * pairs where Opus tagged risk=high but Sonnet didn't (or vice versa)
  * pairs where Opus emitted a NEW divergence_reason category Sonnet missed
  * confidence gap distribution

Run after collect_batch_results() has been called for both batches.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def _bucket(label: str, n: int, total: int) -> str:
    return f"  {label:<46s} {n:5d} ({100.0*n/max(1,total):5.1f}%)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-samples", type=int, default=4,
                        help="N samples to print per disagreement category")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema

    cfg = load_config()
    init_schema(cfg.db_path)

    with connect(cfg.db_path) as conn:
        # For each candidate that has BOTH a Sonnet and an Opus verdict, take
        # the most-recent verdict per (candidate, model).
        rows = conn.execute("""
            WITH sonnet AS (
                SELECT v.*
                FROM pair_verdicts v
                JOIN (
                    SELECT candidate_id, MAX(verdict_ts) AS ts
                    FROM pair_verdicts WHERE model LIKE 'claude-sonnet%'
                    GROUP BY candidate_id
                ) t ON t.candidate_id = v.candidate_id AND t.ts = v.verdict_ts
            ),
            opus AS (
                SELECT v.*
                FROM pair_verdicts v
                JOIN (
                    SELECT candidate_id, MAX(verdict_ts) AS ts
                    FROM pair_verdicts WHERE model LIKE 'claude-opus%'
                    GROUP BY candidate_id
                ) t ON t.candidate_id = v.candidate_id AND t.ts = v.verdict_ts
            )
            SELECT s.candidate_id,
                   s.match AS s_match, s.match_polarity AS s_pol,
                   s.resolution_divergence_risk AS s_risk,
                   s.confidence AS s_conf, s.divergence_reason AS s_reason,
                   s.reasoning AS s_reasoning,
                   o.match AS o_match, o.match_polarity AS o_pol,
                   o.resolution_divergence_risk AS o_risk,
                   o.confidence AS o_conf, o.divergence_reason AS o_reason,
                   o.reasoning AS o_reasoning,
                   m1.title AS k_title, m2.title AS p_title
            FROM sonnet s
            JOIN opus o ON o.candidate_id = s.candidate_id
            JOIN candidate_pairs c ON c.id = s.candidate_id
            JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=c.kalshi_ticker
            JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=c.poly_global_market_id
        """).fetchall()

    n = len(rows)
    if n == 0:
        print("No pairs have both Sonnet and Opus verdicts yet.")
        print("Run poll_batch.py for both batches first.")
        return 1

    print(f"=== Cross-model comparison ({n} pairs with both verdicts) ===\n")

    # ──────────────────────────────────────────────────────────────────
    # Agreement rates on each field
    match_agree = sum(1 for r in rows if r["s_match"] == r["o_match"])
    pol_agree = sum(1 for r in rows if r["s_pol"] == r["o_pol"])
    risk_agree = sum(1 for r in rows if r["s_risk"] == r["o_risk"])
    print("--- exact agreement ---")
    print(_bucket("match", match_agree, n))
    print(_bucket("match_polarity", pol_agree, n))
    print(_bucket("resolution_divergence_risk", risk_agree, n))
    print()

    # ──────────────────────────────────────────────────────────────────
    # Confusion matrix for `match`
    print("--- match confusion matrix (rows=Sonnet, cols=Opus) ---")
    mc = Counter()
    for r in rows:
        mc[(r["s_match"], r["o_match"])] += 1
    matches = sorted({m for r in rows for m in (r["s_match"], r["o_match"])})
    print(f"  {'':10s} | " + "  ".join(f"{m:>10s}" for m in matches))
    for s_m in matches:
        cells = [f"{mc[(s_m, o_m)]:>10d}" for o_m in matches]
        print(f"  {s_m:10s} | " + "  ".join(cells))
    print()

    # ──────────────────────────────────────────────────────────────────
    # Risk confusion matrix
    print("--- risk confusion matrix (rows=Sonnet, cols=Opus) ---")
    rc = Counter()
    for r in rows:
        rc[(r["s_risk"], r["o_risk"])] += 1
    risks = ["none", "low", "medium", "high"]
    print(f"  {'':10s} | " + "  ".join(f"{x:>8s}" for x in risks))
    for s_r in risks:
        cells = [f"{rc[(s_r, o_r)]:>8d}" for o_r in risks]
        print(f"  {s_r:10s} | " + "  ".join(cells))
    print()

    # ──────────────────────────────────────────────────────────────────
    # Polarity confusion matrix
    print("--- polarity confusion matrix (rows=Sonnet, cols=Opus) ---")
    pc = Counter()
    for r in rows:
        pc[(r["s_pol"], r["o_pol"])] += 1
    pols = ["same", "inverse", "unknown"]
    print(f"  {'':10s} | " + "  ".join(f"{x:>9s}" for x in pols))
    for s_p in pols:
        cells = [f"{pc[(s_p, o_p)]:>9d}" for o_p in pols]
        print(f"  {s_p:10s} | " + "  ".join(cells))
    print()

    # ──────────────────────────────────────────────────────────────────
    # Disagreement categories (with samples)
    print("=== Disagreement samples ===\n")

    # 1. Sonnet=ambiguous → Opus=yes (Opus rescued real matches)
    rescued = [r for r in rows if r["s_match"] == "ambiguous" and r["o_match"] == "yes"]
    print(f"--- Sonnet=ambiguous → Opus=yes (rescued matches): {len(rescued)} ---")
    for r in rescued[: args.show_samples]:
        print(f"  cand-{r['candidate_id']}: Opus polarity={r['o_pol']} conf={r['o_conf']:.2f}")
        print(f"    KAL: {(r['k_title'] or '')[:75]}")
        print(f"    POL: {(r['p_title'] or '')[:75]}")
        if r["o_reasoning"]:
            print(f"    OPUS REASON: {(r['o_reasoning'] or '')[:160]}")
        print()

    # 2. Sonnet=yes → Opus=no (Opus rejected false matches)
    rejected = [r for r in rows if r["s_match"] == "yes" and r["o_match"] == "no"]
    print(f"--- Sonnet=yes → Opus=no (Opus rejected fakes): {len(rejected)} ---")
    for r in rejected[: args.show_samples]:
        print(f"  cand-{r['candidate_id']}: Opus says no with reason:")
        print(f"    KAL: {(r['k_title'] or '')[:75]}")
        print(f"    POL: {(r['p_title'] or '')[:75]}")
        if r["o_reason"]:
            print(f"    OPUS DIVERGENCE: {(r['o_reason'] or '')[:160]}")
        if r["o_reasoning"]:
            print(f"    OPUS REASON: {(r['o_reasoning'] or '')[:160]}")
        print()

    # 3. Polarity flipped (Sonnet=same, Opus=inverse) — these are dangerous
    pol_flip_to_inverse = [r for r in rows
                           if r["s_match"] == "yes" and r["o_match"] == "yes"
                           and r["s_pol"] == "same" and r["o_pol"] == "inverse"]
    print(f"--- match=yes, polarity flipped same→inverse: {len(pol_flip_to_inverse)} ---")
    for r in pol_flip_to_inverse[: args.show_samples]:
        print(f"  cand-{r['candidate_id']}: Opus says inverse")
        print(f"    KAL: {(r['k_title'] or '')[:75]}")
        print(f"    POL: {(r['p_title'] or '')[:75]}")
        if r["o_reasoning"]:
            print(f"    OPUS REASON: {(r['o_reasoning'] or '')[:160]}")
        print()

    # 4. Polarity flipped (Sonnet=inverse, Opus=same) — also dangerous
    pol_flip_to_same = [r for r in rows
                        if r["s_match"] == "yes" and r["o_match"] == "yes"
                        and r["s_pol"] == "inverse" and r["o_pol"] == "same"]
    print(f"--- match=yes, polarity flipped inverse→same: {len(pol_flip_to_same)} ---")
    for r in pol_flip_to_same[: args.show_samples]:
        print(f"  cand-{r['candidate_id']}")
        print(f"    KAL: {(r['k_title'] or '')[:75]}")
        print(f"    POL: {(r['p_title'] or '')[:75]}")
        if r["o_reasoning"]:
            print(f"    OPUS REASON: {(r['o_reasoning'] or '')[:160]}")
        print()

    # 5. Risk escalation: Sonnet low/none → Opus medium/high
    risk_up = [r for r in rows
               if r["s_risk"] in ("none", "low") and r["o_risk"] in ("medium", "high")]
    print(f"--- risk escalated low→medium+ by Opus: {len(risk_up)} ---")
    for r in risk_up[: args.show_samples]:
        print(f"  cand-{r['candidate_id']}: Sonnet={r['s_risk']} Opus={r['o_risk']}")
        print(f"    KAL: {(r['k_title'] or '')[:75]}")
        print(f"    POL: {(r['p_title'] or '')[:75]}")
        if r["o_reason"]:
            print(f"    OPUS DIVERGENCE: {(r['o_reason'] or '')[:160]}")
        print()

    # ──────────────────────────────────────────────────────────────────
    # Pattern discovery: Opus divergence_reasons that don't appear in Sonnet
    print("=== Opus divergence_reason language frequency on disagreements ===")
    # Tokenize Opus's reasoning text on disagreement pairs and surface
    # interesting nouns/phrases that appeared in Opus but seem absent in Sonnet's
    # corresponding text. Crude but useful for finding "what does Opus catch?"
    interesting_words: Counter = Counter()
    for r in rows:
        agree = (r["s_match"] == r["o_match"] and r["s_pol"] == r["o_pol"]
                 and r["s_risk"] == r["o_risk"])
        if agree:
            continue
        for t in (r["o_reason"] or "").lower().split():
            if len(t) >= 6 and t.isalpha():
                interesting_words[t] += 1
    print("  top 25 word stems (length>=6) in Opus divergence reasons on disagreements:")
    for w, count in interesting_words.most_common(25):
        print(f"    {count:4d}  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
