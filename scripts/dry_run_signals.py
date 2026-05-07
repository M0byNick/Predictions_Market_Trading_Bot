"""One-shot signal scan against current quotes.

Identical to what the bot's runloop does each cycle, but ad-hoc + verbose.

Usage:
    .venv/bin/python scripts/dry_run_signals.py              # read-only report
    .venv/bin/python scripts/dry_run_signals.py --top 50     # show top 50 signals
    .venv/bin/python scripts/dry_run_signals.py --commit     # also write paper_signals + paper_fills
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ARB_ROOT = _HERE.parent
_SRC = _ARB_ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=20,
                        help="N top signals to display (default 20)")
    parser.add_argument("--commit", action="store_true",
                        help="Also write paper_signals + paper_fills "
                             "(mirrors a runloop cycle).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    load_dotenv(_ARB_ROOT / ".env", override=True)
    if os.environ.get("DATA_DIR", "").startswith("/app"):
        os.environ["DATA_DIR"] = str(_ARB_ROOT / "data")

    from arb_bot.config import load_config
    from arb_bot.db import connect, init_schema
    from arb_bot.executor.paper import simulate_fill
    from arb_bot.risk.limits import check as risk_check
    from arb_bot.signal.spread import detect_for_pair, record_signal

    cfg = load_config()
    init_schema(cfg.db_path)

    print(f"=== Dry-run signal scan ({'COMMIT' if args.commit else 'READ-ONLY'}) ===")
    print(f"  bankroll          : ${cfg.initial_bankroll_usd:.0f}")
    print(f"  per-pair target   : ${cfg.paper_per_pair_target_usd:.2f}")
    print(f"  max position      : ${cfg.paper_max_position_usd:.2f}")
    print(f"  daily loss stop   : ${cfg.paper_daily_max_loss_usd:.2f}")
    print(f"  min edge bps      : {cfg.paper_min_edge_bps}")
    print()

    with connect(cfg.db_path) as conn:
        approved = conn.execute(
            "SELECT * FROM approved_pairs WHERE active=1"
        ).fetchall()
        n_approved = len(approved)
        print(f"  approved pairs    : {n_approved}")

        signals = []
        n_missing = 0
        for p in approved:
            sig = detect_for_pair(conn, cfg, p)
            if sig is None:
                n_missing += 1
                continue
            signals.append((p, sig))

        n_sig = len(signals)
        n_trade = sum(1 for _, s in signals if s.would_trade)
        n_unknown = sum(1 for _, s in signals if s.polarity == "unknown")
        print(f"  signals generated : {n_sig}")
        print(f"  would trade       : {n_trade}")
        print(f"  missing quotes    : {n_missing}")
        print(f"  polarity unknown  : {n_unknown}")
        print()

        # Reject-reason histogram
        from collections import Counter
        reasons = Counter()
        for _, s in signals:
            if not s.would_trade:
                reasons[s.reject_reason or "(unknown)"] += 1
        if reasons:
            print("=== Reject reasons ===")
            for r, n in reasons.most_common():
                print(f"  {n:5d}  {r}")
            print()

        # Top would-trade signals — sort by ANNUALIZED edge so faster-resolving
        # pairs surface first. A 5% edge in 30 days (~80% APY) beats 5% in
        # 18 months (~3% APY) for the same capital lock.
        trade_sigs = sorted(
            [(p, s) for p, s in signals if s.would_trade],
            key=lambda x: -(x[1].annualized_edge_bps or x[1].fee_adjusted_edge_bps),
        )[: args.top]
        if trade_sigs:
            print(f"=== Top {len(trade_sigs)} would-trade signals (by ANNUALIZED edge) ===")
            print(f"  {'pol':<7s} {'k_yes':>6s} {'p_yes':>6s} "
                  f"{'edge':>6s} {'days':>5s} {'APY':>7s} "
                  f"{'units':>6s} {'capital':>8s}  pair")
            for p, s in trade_sigs:
                pid = s.pair_id[:55]
                days_str = f"{s.days_to_resolve:.0f}" if s.days_to_resolve is not None else "?"
                apy_str = f"{(s.annualized_edge_bps or 0)/100:.0f}%" if s.annualized_edge_bps else "?"
                print(f"  {s.polarity:<7s} {s.kalshi_yes_mid:>6.3f} "
                      f"{s.poly_yes_mid:>6.3f} {s.fee_adjusted_edge_bps:>5.0f}bp "
                      f"{days_str:>5s}d {apy_str:>7s} "
                      f"{s.size_units:>6d} ${s.target_capital_usd:>7.2f}  {pid}")
            print()

            total_capital = sum(s.target_capital_usd for _, s in trade_sigs)
            total_profit = sum(
                s.target_capital_usd * s.fee_adjusted_edge_bps / 10_000
                for _, s in trade_sigs
            )
            print(f"  total capital deployed : ${total_capital:>10.2f}")
            print(f"  expected gross profit  : ${total_profit:>10.2f}  "
                  f"({100.0 * total_profit / total_capital:.2f}% ROI on cycle)"
                  if total_capital else "  (no would-trade signals)")
        else:
            print("No would-trade signals — try refreshing quotes via the "
                  "hourly cron, or lower PAPER_MIN_EDGE_BPS.")

        if not args.commit:
            print()
            print("READ-ONLY: no rows written. Use --commit to persist.")
            return 0

        # Commit path: mirrors main.py cycle.
        # Per-row commit + exception isolation: a single lock contention
        # against the dashboard or hourly ingest costs us 1 row, not the
        # whole cycle. Without this, sqlite WAL contention can roll back
        # 400+ accumulated signals.
        #
        # Pre-fill validation: every would_trade signal is re-checked
        # against fresh live bid/ask before simulate_fill. The cycle's
        # cached prices are mid-based and may not reflect realistic
        # executable spread (especially for thin Polymarket books or
        # stale Kalshi quotes). validate_pair_now() does ~2 HTTP calls
        # per signal -- cheap because would_trade signals are 5-10 per
        # cycle, not 460.
        import sqlite3 as _sqlite3
        from arb_bot.signal.validate import validate_pair_now
        print()
        print("=== Committing signals + fills ===")
        n_recorded = 0
        n_filled = 0
        n_blocked = 0
        n_locked = 0
        n_validation_skip = 0
        for p, sig in signals:
            try:
                if sig.would_trade:
                    # Pre-flight: re-validate with fresh quotes
                    v = validate_pair_now(conn=conn, cfg=cfg, pair_id=sig.pair_id)
                    if not v.is_arb_now:
                        # Override the would_trade flag so the row records
                        # accurately as a no-trade with the validation reason.
                        sig.would_trade = False
                        sig.reject_reason = (
                            f"pre-flight validation: {v.reason} "
                            f"(exec_edge={v.executable_edge_bps:+.0f}bps)"
                        )
                        n_validation_skip += 1
                        logging.info(
                            "skip on validation %s: %s",
                            sig.pair_id[:60], v.reason,
                        )
                sig_id = record_signal(conn, sig)
                if sig.would_trade:
                    ok, reason = risk_check(conn, cfg, sig.pair_id)
                    if ok:
                        simulate_fill(conn, sig_id, sig)
                        n_filled += 1
                    else:
                        n_blocked += 1
                conn.commit()
                n_recorded += 1
            except _sqlite3.OperationalError as e:
                conn.rollback()
                n_locked += 1
                logging.warning(
                    "skipping %s: %s (continuing cycle)",
                    sig.pair_id[:60], e,
                )
        print(f"  signals recorded     : {n_recorded}/{len(signals)}")
        print(f"  paper-filled         : {n_filled}")
        print(f"  risk-blocked         : {n_blocked}")
        print(f"  skipped on validation: {n_validation_skip}  (mid-deceptive cached spread)")
        if n_locked:
            print(f"  skipped on lock      : {n_locked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
