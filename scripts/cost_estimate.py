"""Project ongoing operational cost at different cadences.

Cost decomposes into:
  1. Market-data ingest             FREE (hits public Kalshi/Poly endpoints)
  2. Embedding new markets          FREE (local sentence-transformers, MPS)
  3. Cosine candidate generation    FREE (numpy matmul)
  4. LLM adjudication of new pairs  $$ (Anthropic API)
  5. Storage / disk                 ~negligible

Only #4 is non-trivial. We model the dollar cost based on:
  * incoming new-market rate per cycle
  * top-K candidates per new market (default 5)
  * cost-per-pair on the chosen Anthropic model

Default new-market estimates are conservative based on the first
ingest's data: ~5K Kalshi + ~5K Polymarket open markets at any time,
with ~3-5% turnover daily (markets close + new ones open).
"""
from __future__ import annotations

import argparse


# Anthropic Batch API (50% off standard) circa 2026
SONNET_INPUT_PER_M = 1.50
SONNET_OUTPUT_PER_M = 7.50
OPUS_INPUT_PER_M = 7.50
OPUS_OUTPUT_PER_M = 37.50

# Tokens per pair (empirical from 18854-pair seed batch)
TOKENS_INPUT = 600
TOKENS_OUTPUT = 175


def cost_per_pair(model: str) -> tuple[float, float, float]:
    """Returns (input_cost, output_cost, total) per pair."""
    if "opus" in model.lower():
        ip = TOKENS_INPUT * OPUS_INPUT_PER_M / 1_000_000
        op = TOKENS_OUTPUT * OPUS_OUTPUT_PER_M / 1_000_000
    else:
        ip = TOKENS_INPUT * SONNET_INPUT_PER_M / 1_000_000
        op = TOKENS_OUTPUT * SONNET_OUTPUT_PER_M / 1_000_000
    return ip, op, ip + op


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new-markets-per-day", type=int, default=200,
                        help="New open markets per day across both venues "
                             "(typical 50-300 in steady state)")
    parser.add_argument("--top-k", type=int, default=5,
                        help="Cosine top-K candidates per new market")
    parser.add_argument("--model", type=str, default="claude-sonnet-4-5",
                        help="Adjudicator model")
    parser.add_argument("--include-opus-audit-frequency", type=str, default="weekly",
                        choices=("never", "weekly", "monthly"),
                        help="How often to spot-check w/ Opus on the suspect tiers")
    args = parser.parse_args()

    print("== Cost projection at various cadences ==")
    print()
    print(f"  assumptions:")
    print(f"    new markets/day    : {args.new_markets_per_day}")
    print(f"    top-K per market   : {args.top_k}")
    print(f"    new pairs/day      : {args.new_markets_per_day * args.top_k:,}")
    print(f"    adjudicator model  : {args.model}")
    print(f"    Opus audit cadence : {args.include_opus_audit_frequency}")

    new_pairs_per_day = args.new_markets_per_day * args.top_k
    cpp_in, cpp_out, cpp = cost_per_pair(args.model)
    print(f"    cost per pair      : ${cpp:.5f}  ({args.model} batch)")
    print()

    print("  ───────────────────────────────────────────────────────────────")
    print(f"  {'cadence':18s} {'new pairs/cycle':>17s} {'$/cycle':>10s} {'$/day':>9s} {'$/month':>10s}")
    print("  ───────────────────────────────────────────────────────────────")

    cadences = [
        ("hourly (24x/day)", 24),
        ("every 4h (6x/day)", 6),
        ("every 8h (3x/day)", 3),
        ("every 12h (2x/day)", 2),
        ("daily", 1),
        ("every 3 days", 1/3),
        ("weekly", 1/7),
    ]
    daily_cost = new_pairs_per_day * cpp
    for label, cycles_per_day in cadences:
        per_cycle_pairs = new_pairs_per_day / max(cycles_per_day, 1e-9)
        per_cycle_cost = per_cycle_pairs * cpp
        per_month_cost = daily_cost * 30
        print(f"  {label:18s} {per_cycle_pairs:>17,.0f} ${per_cycle_cost:>9.3f} ${daily_cost:>8.2f} ${per_month_cost:>9.2f}")

    print("  ───────────────────────────────────────────────────────────────")
    print()

    # Opus audit sweep — if we Opus the suspect tiers periodically
    suspect_pairs_estimate = 800  # ambiguous + review-recommended typical size
    opus_cpp_total = sum(cost_per_pair("claude-opus-4-7")[0:2])
    opus_audit_per_run = suspect_pairs_estimate * opus_cpp_total
    print(f"  Opus suspect-tier audit (~{suspect_pairs_estimate} pairs × Opus rates)")
    print(f"    per audit run     : ~${opus_audit_per_run:.2f}")
    if args.include_opus_audit_frequency == "weekly":
        print(f"    weekly             : ~${opus_audit_per_run:.2f}/wk = ~${opus_audit_per_run * 4.3:.2f}/mo")
    elif args.include_opus_audit_frequency == "monthly":
        print(f"    monthly            : ~${opus_audit_per_run:.2f}/mo")
    print()

    print("  ───────────────────────────────────────────────────────────────")
    print("  TOTAL MONTHLY ESTIMATE (Sonnet daily + Opus weekly audit)")
    sonnet_daily_monthly = daily_cost * 30
    opus_monthly = opus_audit_per_run * 4.3 if args.include_opus_audit_frequency == "weekly" else (opus_audit_per_run if args.include_opus_audit_frequency == "monthly" else 0)
    total = sonnet_daily_monthly + opus_monthly
    print(f"    Sonnet daily ingest : ${sonnet_daily_monthly:.2f}/mo")
    print(f"    Opus periodic audit : ${opus_monthly:.2f}/mo")
    print(f"    TOTAL               : ${total:.2f}/mo")
    print("  ───────────────────────────────────────────────────────────────")
    print()
    print("  Recommendations:")
    print("    - Decouple data ingest (every 1-2h, free) from LLM adjudication (every 12-24h)")
    print("    - Embeddings are local + free; run after every ingest")
    print("    - Save Opus for weekly audit on the safest+review tiers (highest-impact spend)")
    print("    - Setup a daily cron for the seed_candidates.py pipeline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
