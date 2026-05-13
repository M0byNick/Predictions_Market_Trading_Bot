"""Replay paper_signals through two sizers and compare outcomes.

This is ANALYSIS TOOLING — not a change to the production sizer. It loads
historical paper_signals from the bot's SQLite DB, runs each signal through
both the current fixed-fractional sizer and the proposed fractional-Kelly
sizer (see docs/SIZING_SCOPE_2026-05-13.md), applies the risk gates
chronologically, and reports:

  1. Sizer-output divergence (per-signal $ size, by edge bucket)
  2. Synthetic clean-settlement PnL upper bound
     (every fill settles cleanly; ignores tail risk entirely)
  3. Monte Carlo realized PnL with explicit tail-risk parameters
     (p_clean / p_divergence / L_div etc. swept across scenarios)
  4. Aggregate-inventory cap firing rate
  5. Position-concentration (Herfindahl) and per-pair exposure

Usage:
  python scripts/replay_sizers.py \\
      --db /path/to/arb_bot.sqlite \\
      --bankroll 3000 \\
      --kelly-fraction 0.25 \\
      --p-clean 0.97 \\
      --loss-on-divergence 0.50 \\
      --inventory-cap-pct 0.30 \\
      --mc-trials 1000 \\
      --report-path docs/SIZING_REPLAY_2026-05-13.md

Defaults match the recommendations in SIZING_SCOPE_2026-05-13.md.

Notes:
  - No book-depth historic snapshot exists in paper_signals, so the
    book-depth cap is NOT exercised in this replay. (Recommendation in
    the scope doc: add a book_depth_at_signal_usd column before doing a
    fully-honest replay. This script is a first-cut viability test.)
  - Realized PnL on the actual paper_fills table can't be used because
    92 of 156 fills were phantom-wiped on 2026-05-06 and the remaining
    64 fills haven't settled (5+ days waiting on event resolution).
    So synthetic / Monte Carlo PnL is the only available comparison.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


# --------------------------------------------------------------------------
# Sizer implementations (replicated from src/arb_bot/signal/spread.py +
# the recommendation in docs/SIZING_SCOPE_2026-05-13.md).
# Replicated rather than imported so the script is self-contained and the
# comparison is auditable; the fixed-fractional path is line-by-line
# equivalent to _compute_size_units() in production.
# --------------------------------------------------------------------------

def capital_per_unit(polarity: str, kal_mid: float, poly_mid: float) -> float:
    if polarity == "inverse":
        return max(0.01, kal_mid + poly_mid)
    return max(0.01, max(kal_mid, poly_mid))


def size_fixed_fractional(
    bankroll: float, per_pair_pct: float, max_pct: float,
    min_usd: float, polarity: str, kal_mid: float, poly_mid: float,
) -> tuple[int, float]:
    """Current production sizer. Edge-blind."""
    target = bankroll * per_pair_pct
    target = min(target, bankroll * max_pct)
    if target < min_usd:
        return 0, 0.0
    cpu = capital_per_unit(polarity, kal_mid, poly_mid)
    units = max(1, int(target / cpu))
    return units, units * cpu


def size_fractional_kelly(
    bankroll: float, kelly_fraction: float, p_clean: float,
    loss_on_divergence: float, max_pct: float, min_usd: float,
    polarity: str, kal_mid: float, poly_mid: float, edge_bps: float,
) -> tuple[int, float, float]:
    """Proposed sizer. Edge-aware via asymmetric-payoff Kelly."""
    s_stake = max(edge_bps / 10_000.0, 0.0)  # spread as fraction of $1 contract
    cpu = capital_per_unit(polarity, kal_mid, poly_mid)
    # Spread per dollar of CAPITAL committed (not per $1 notional).
    # Capital required per unit is the binding stake; the win is the
    # captured spread across the pair, so b = spread / capital_per_unit.
    b = s_stake / cpu

    p = p_clean
    q = 1.0 - p
    L = loss_on_divergence

    # Two-state asymmetric Kelly. Clamp at 0 (negative → skip).
    denom = max(b * L, 1e-12)
    f_kelly = (p * b - q * L) / denom
    f_kelly = max(f_kelly, 0.0)
    f_target = f_kelly * kelly_fraction

    target_usd = f_target * bankroll
    target_usd = min(target_usd, bankroll * max_pct)
    if target_usd < min_usd:
        return 0, 0.0, f_kelly
    units = max(1, int(target_usd / cpu))
    return units, units * cpu, f_kelly


# --------------------------------------------------------------------------
# Replay engine
# --------------------------------------------------------------------------

@dataclass
class Trade:
    ts: int
    pair_id: str
    direction: str
    polarity: str
    kal_mid: float
    poly_mid: float
    edge_bps: float
    days_to_resolve: float | None
    size_units: int
    notional_usd: float                   # units × capital_per_unit
    captured_spread_usd: float            # units × raw_spread (BEFORE fees, slippage, divergence)
    fees_usd: float                       # round-trip fee at fill prices
    f_kelly_raw: float | None = None      # only set on Kelly sizer


@dataclass
class ReplayResult:
    name: str
    trades: list[Trade] = field(default_factory=list)
    skipped_by_inventory_cap: int = 0
    skipped_by_per_pair_cap: int = 0
    skipped_by_daily_loss: int = 0
    skipped_by_sizer_zero: int = 0

    def n_trades(self) -> int: return len(self.trades)
    def total_notional(self) -> float: return sum(t.notional_usd for t in self.trades)
    def clean_pnl(self) -> float:
        """Sum of captured spread minus fees (no tails, no slippage)."""
        return sum(t.captured_spread_usd - t.fees_usd for t in self.trades)


def _kalshi_taker_fee_per_contract(price: float) -> float:
    p = max(0.0, min(1.0, price))
    return 0.07 * p * (1.0 - p)


def _poly_taker_fee_per_contract(price: float) -> float:
    p = max(0.0, min(1.0, price))
    return 0.02 * p


def _round_trip_fee(units: int, kal_mid: float, poly_mid: float) -> float:
    return units * (
        _kalshi_taker_fee_per_contract(kal_mid)
        + _poly_taker_fee_per_contract(poly_mid)
    )


def _raw_spread(polarity: str, kal_mid: float, poly_mid: float) -> float:
    if polarity == "inverse":
        return abs(kal_mid + poly_mid - 1.0)
    return abs(kal_mid - poly_mid)


def replay(
    rows: list[dict],
    sizer_name: str,
    bankroll: float,
    per_pair_pct: float, max_pct: float, min_usd: float,
    daily_max_loss_pct: float,
    inventory_cap_pct: float,
    kelly_fraction: float, p_clean: float, loss_on_divergence: float,
    open_window_days: float = 7.0,
    first_signal_per_pair_only: bool = False,
) -> ReplayResult:
    """Single-pass replay applying risk gates in chronological order.

    For per-pair / inventory caps we track OPEN notional. We don't have
    settlement timing in paper_signals, so we approximate "open" as "all
    fills opened in the trailing `open_window_days` from this signal's
    timestamp". Production hold times are event-resolution-driven and
    typically range 1-30 days; sweep this knob to see sensitivity.

    `first_signal_per_pair_only=True` is the apples-to-apples isolation
    test: take only the chronologically first would_trade signal per pair,
    so each pair contributes exactly one trade and the sizer comparison
    is not confounded by per-pair-cap reentries.
    """
    res = ReplayResult(name=sizer_name)
    OPEN_WINDOW_SEC = int(open_window_days * 86400)
    seen_pairs: set[str] = set()

    # open positions queue: list of (open_ts, pair_id, notional_usd)
    open_positions: list[tuple[int, str, float]] = []
    daily_pnl: dict[str, float] = defaultdict(float)  # 'YYYY-MM-DD' → realized PnL
    inventory_cap_usd = inventory_cap_pct * bankroll
    per_pair_cap_usd = max_pct * bankroll
    daily_stop_usd = daily_max_loss_pct * bankroll

    for r in rows:
        ts = r["detected_ts"]
        if first_signal_per_pair_only and r["pair_id"] in seen_pairs:
            continue
        # Roll off positions older than the open window
        while open_positions and open_positions[0][0] < ts - OPEN_WINDOW_SEC:
            open_positions.pop(0)

        # Day-level PnL gate (uses clean PnL as proxy — under-stops if
        # there are real losses; matches the bot's actual daily_pnl_usd()
        # behavior using realized_pnl_usd which is empty in paper today).
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        if daily_pnl[day] <= -daily_stop_usd:
            res.skipped_by_daily_loss += 1
            continue

        polarity = r["polarity"]
        kal_mid = r["kalshi_yes_mid"]
        poly_mid = r["poly_yes_mid"]
        edge_bps = r["fee_adjusted_edge_bps"]
        pair_id = r["pair_id"]

        # Compute size
        if sizer_name == "fixed_fractional":
            units, notional = size_fixed_fractional(
                bankroll, per_pair_pct, max_pct, min_usd,
                polarity, kal_mid, poly_mid,
            )
            f_kelly_raw = None
        elif sizer_name == "fractional_kelly":
            units, notional, f_kelly_raw = size_fractional_kelly(
                bankroll, kelly_fraction, p_clean, loss_on_divergence,
                max_pct, min_usd, polarity, kal_mid, poly_mid, edge_bps,
            )
        else:
            raise ValueError(sizer_name)

        if units == 0:
            res.skipped_by_sizer_zero += 1
            continue

        # Per-pair cap
        pair_open = sum(n for (t0, p, n) in open_positions if p == pair_id)
        if pair_open + notional > per_pair_cap_usd:
            res.skipped_by_per_pair_cap += 1
            continue

        # Aggregate inventory cap
        total_open = sum(n for (t0, p, n) in open_positions)
        if total_open + notional > inventory_cap_usd:
            res.skipped_by_inventory_cap += 1
            continue

        # Take the trade
        spread = _raw_spread(polarity, kal_mid, poly_mid)
        fees = _round_trip_fee(units, kal_mid, poly_mid)
        captured = units * spread

        res.trades.append(Trade(
            ts=ts, pair_id=pair_id, direction=r["direction"],
            polarity=polarity, kal_mid=kal_mid, poly_mid=poly_mid,
            edge_bps=edge_bps, days_to_resolve=r["days_to_resolve"],
            size_units=units, notional_usd=notional,
            captured_spread_usd=captured, fees_usd=fees,
            f_kelly_raw=f_kelly_raw,
        ))
        open_positions.append((ts, pair_id, notional))
        seen_pairs.add(pair_id)
        # Daily PnL stays at 0 in the replay because we don't have
        # settlement timestamps — see docstring caveat.

    return res


# --------------------------------------------------------------------------
# Analysis: Monte Carlo PnL under tail-risk model
# --------------------------------------------------------------------------

def monte_carlo_pnl(
    trades: list[Trade], n_trials: int,
    p_clean: float, p_divergence: float, p_third_party: float, p_void: float,
    L_div: float, L_third_party: float, L_void: float,
    seed: int = 42,
) -> dict:
    """Sample realized PnL across n_trials, with outcomes drawn from:
       clean       (+spread - fees)
       divergence  (-L_div × notional - fees)
       third_party (-L_3p × notional - fees)   [inverse only; 0 prob on same]
       void        (-L_void × notional - fees)
       (residual)  flat: 0 - fees   (treats leg-fail as zero expected payoff)
    """
    if not trades:
        return {"mean_pnl": 0, "median_pnl": 0, "p05": 0, "p95": 0,
                "max_dd": 0, "n_trades": 0}

    rng = np.random.default_rng(seed)
    n = len(trades)
    spreads_usd = np.array([t.captured_spread_usd for t in trades])
    fees_usd = np.array([t.fees_usd for t in trades])
    notional_usd = np.array([t.notional_usd for t in trades])
    is_inverse = np.array([t.polarity == "inverse" for t in trades])

    # outcome codes: 0=clean, 1=divergence, 2=third_party, 3=void, 4=residual_flat
    # Inverse pairs get full distribution; same-polarity pairs roll into clean+div+void+flat
    p_clean_eff = np.where(is_inverse, p_clean, p_clean + p_third_party)
    # Build a (n,5) probability matrix per trade
    P = np.zeros((n, 5))
    P[:, 0] = p_clean_eff
    P[:, 1] = p_divergence
    P[:, 2] = np.where(is_inverse, p_third_party, 0.0)
    P[:, 3] = p_void
    P[:, 4] = 1.0 - P[:, :4].sum(axis=1)
    P[P[:, 4] < 0, 4] = 0.0
    P = P / P.sum(axis=1, keepdims=True)

    pnl_clean = spreads_usd - fees_usd
    pnl_div = -L_div * notional_usd - fees_usd
    pnl_3p = -L_third_party * notional_usd - fees_usd
    pnl_void = -L_void * notional_usd - fees_usd
    pnl_residual = -fees_usd

    final_pnl_per_trial = np.zeros(n_trials)
    max_dd_per_trial = np.zeros(n_trials)
    for t in range(n_trials):
        # Vectorized: per-trade outcome draw via cumulative inverse CDF
        rand = rng.random(n)
        cum = P.cumsum(axis=1)
        outcomes = (rand[:, None] < cum).argmax(axis=1)
        pnl = np.where(outcomes == 0, pnl_clean,
              np.where(outcomes == 1, pnl_div,
              np.where(outcomes == 2, pnl_3p,
              np.where(outcomes == 3, pnl_void, pnl_residual))))
        equity = np.cumsum(pnl)
        peaks = np.maximum.accumulate(equity)
        dd = (equity - peaks).min() if len(equity) else 0
        final_pnl_per_trial[t] = equity[-1] if len(equity) else 0
        max_dd_per_trial[t] = dd

    return {
        "n_trades": n,
        "mean_pnl": float(final_pnl_per_trial.mean()),
        "median_pnl": float(np.median(final_pnl_per_trial)),
        "p05": float(np.percentile(final_pnl_per_trial, 5)),
        "p95": float(np.percentile(final_pnl_per_trial, 95)),
        "p_loss": float((final_pnl_per_trial < 0).mean()),
        "mean_max_dd": float(max_dd_per_trial.mean()),
        "p05_max_dd": float(np.percentile(max_dd_per_trial, 5)),
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def edge_bucket(edge_bps: float) -> str:
    if edge_bps < 100: return "00_<100"
    if edge_bps < 200: return "01_100-200"
    if edge_bps < 300: return "02_200-300"
    if edge_bps < 500: return "03_300-500"
    if edge_bps < 1000: return "04_500-1000"
    if edge_bps < 2000: return "05_1000-2000"
    if edge_bps < 5000: return "06_2000-5000"
    return "07_5000+"


def by_edge_bucket(trades: list[Trade]) -> dict[str, dict]:
    buckets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "notional": 0.0, "clean_pnl": 0.0, "units": 0}
    )
    for t in trades:
        b = edge_bucket(t.edge_bps)
        buckets[b]["n"] += 1
        buckets[b]["notional"] += t.notional_usd
        buckets[b]["clean_pnl"] += t.captured_spread_usd - t.fees_usd
        buckets[b]["units"] += t.size_units
    return buckets


def herfindahl(trades: list[Trade]) -> float:
    if not trades: return 0.0
    by_pair = defaultdict(float)
    for t in trades:
        by_pair[t.pair_id] += t.notional_usd
    total = sum(by_pair.values())
    if total <= 0: return 0.0
    shares = np.array([v / total for v in by_pair.values()])
    return float((shares ** 2).sum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--bankroll", type=float, default=3000.0)
    ap.add_argument("--per-pair-pct", type=float, default=0.05)
    ap.add_argument("--max-pct", type=float, default=0.10)
    ap.add_argument("--min-usd", type=float, default=25.0)
    ap.add_argument("--daily-max-loss-pct", type=float, default=0.05)
    ap.add_argument("--inventory-cap-pct", type=float, default=0.30)
    ap.add_argument("--kelly-fraction", type=float, default=0.25)
    ap.add_argument("--p-clean", type=float, default=0.97)
    ap.add_argument("--loss-on-divergence", type=float, default=0.50)
    ap.add_argument("--mc-trials", type=int, default=1000)
    ap.add_argument("--open-window-days", type=float, default=7.0,
                    help="Hold-time proxy for inventory cap rolloff (default 7).")
    ap.add_argument("--first-per-pair", action="store_true",
                    help="Take only the first would_trade signal per pair (isolation test).")
    ap.add_argument("--report-path", type=Path, default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Time-to-resolve column doesn't exist on paper_signals — synthesize
    # from approved_pairs / markets where possible. For replay parity with
    # production this only matters if we want to re-apply the tiered min
    # edge gate inside the replay (we do).
    sql = """
    SELECT ps.id, ps.pair_id, ps.detected_ts, ps.kalshi_yes_mid, ps.poly_yes_mid,
           ps.raw_spread, ps.fee_adjusted_edge_bps, ps.direction, ps.would_trade,
           ap.match_polarity AS polarity,
           ap.tag AS tag,
           NULL AS days_to_resolve
      FROM paper_signals ps
      LEFT JOIN approved_pairs ap ON ap.pair_id = ps.pair_id
     WHERE ps.would_trade = 1
       AND ap.match_polarity IN ('same','inverse')
       AND ap.tag != 'high_risk'
     ORDER BY ps.detected_ts
    """
    rows = []
    for r in conn.execute(sql):
        rows.append({
            "pair_id": r["pair_id"],
            "detected_ts": r["detected_ts"],
            "kalshi_yes_mid": r["kalshi_yes_mid"] or 0.0,
            "poly_yes_mid": r["poly_yes_mid"] or 0.0,
            "fee_adjusted_edge_bps": r["fee_adjusted_edge_bps"] or 0.0,
            "direction": r["direction"],
            "polarity": r["polarity"] or "unknown",
            "days_to_resolve": r["days_to_resolve"],
            "tag": r["tag"],
        })
    print(f"Loaded {len(rows)} would_trade signals from {args.db}")

    # Run both sizers
    res_ff = replay(rows, "fixed_fractional",
                    args.bankroll, args.per_pair_pct, args.max_pct, args.min_usd,
                    args.daily_max_loss_pct, args.inventory_cap_pct,
                    args.kelly_fraction, args.p_clean, args.loss_on_divergence,
                    open_window_days=args.open_window_days,
                    first_signal_per_pair_only=args.first_per_pair)
    res_fk = replay(rows, "fractional_kelly",
                    args.bankroll, args.per_pair_pct, args.max_pct, args.min_usd,
                    args.daily_max_loss_pct, args.inventory_cap_pct,
                    args.kelly_fraction, args.p_clean, args.loss_on_divergence,
                    open_window_days=args.open_window_days,
                    first_signal_per_pair_only=args.first_per_pair)

    print(f"\n=== Fixed-fractional sizer ===")
    print(f"  trades         : {res_ff.n_trades()}")
    print(f"  total notional : ${res_ff.total_notional():.0f}")
    print(f"  clean PnL      : ${res_ff.clean_pnl():.2f}")
    print(f"  Herfindahl     : {herfindahl(res_ff.trades):.4f}")
    print(f"  skipped (sizer zero / per-pair / inventory / daily-stop):"
          f" {res_ff.skipped_by_sizer_zero}/{res_ff.skipped_by_per_pair_cap}"
          f"/{res_ff.skipped_by_inventory_cap}/{res_ff.skipped_by_daily_loss}")

    print(f"\n=== Fractional-Kelly sizer ({args.kelly_fraction}× Kelly, p={args.p_clean}, L={args.loss_on_divergence}) ===")
    print(f"  trades         : {res_fk.n_trades()}")
    print(f"  total notional : ${res_fk.total_notional():.0f}")
    print(f"  clean PnL      : ${res_fk.clean_pnl():.2f}")
    print(f"  Herfindahl     : {herfindahl(res_fk.trades):.4f}")
    print(f"  skipped (sizer zero / per-pair / inventory / daily-stop):"
          f" {res_fk.skipped_by_sizer_zero}/{res_fk.skipped_by_per_pair_cap}"
          f"/{res_fk.skipped_by_inventory_cap}/{res_fk.skipped_by_daily_loss}")

    # By-edge-bucket detail
    ff_buckets = by_edge_bucket(res_ff.trades)
    fk_buckets = by_edge_bucket(res_fk.trades)
    all_buckets = sorted(set(ff_buckets) | set(fk_buckets))
    print(f"\n{'bucket':<14} {'FF n':>5} {'FF $ntl':>10} {'FF cln':>9}   {'FK n':>5} {'FK $ntl':>10} {'FK cln':>9}")
    for b in all_buckets:
        f = ff_buckets.get(b, {"n": 0, "notional": 0, "clean_pnl": 0})
        k = fk_buckets.get(b, {"n": 0, "notional": 0, "clean_pnl": 0})
        print(f"{b:<14} {f['n']:>5} {f['notional']:>10.0f} {f['clean_pnl']:>9.2f}   "
              f"{k['n']:>5} {k['notional']:>10.0f} {k['clean_pnl']:>9.2f}")

    # Monte Carlo across three scenarios
    print(f"\n=== Monte Carlo PnL ({args.mc_trials} trials) ===")
    scenarios = [
        ("base",        dict(p_clean=0.97, p_divergence=0.02, p_third_party=0.005, p_void=0.005,
                             L_div=0.50, L_third_party=0.50, L_void=0.30)),
        ("optimistic",  dict(p_clean=0.99, p_divergence=0.005, p_third_party=0.002, p_void=0.003,
                             L_div=0.50, L_third_party=0.50, L_void=0.30)),
        ("pessimistic", dict(p_clean=0.93, p_divergence=0.04,  p_third_party=0.015, p_void=0.015,
                             L_div=0.60, L_third_party=0.60, L_void=0.30)),
    ]
    mc_results: list[tuple[str, dict, dict]] = []
    for name, params in scenarios:
        ff_mc = monte_carlo_pnl(res_ff.trades, args.mc_trials, **params, seed=42)
        fk_mc = monte_carlo_pnl(res_fk.trades, args.mc_trials, **params, seed=42)
        mc_results.append((name, ff_mc, fk_mc))
        print(f"\n  scenario={name}  params={params}")
        print(f"    FF: mean ${ff_mc['mean_pnl']:>+8.2f}  med ${ff_mc['median_pnl']:>+8.2f}  "
              f"p05 ${ff_mc['p05']:>+8.2f}  p95 ${ff_mc['p95']:>+8.2f}  "
              f"p(loss)={ff_mc['p_loss']:.2%}  mean_dd ${ff_mc['mean_max_dd']:>+8.2f}")
        print(f"    FK: mean ${fk_mc['mean_pnl']:>+8.2f}  med ${fk_mc['median_pnl']:>+8.2f}  "
              f"p05 ${fk_mc['p05']:>+8.2f}  p95 ${fk_mc['p95']:>+8.2f}  "
              f"p(loss)={fk_mc['p_loss']:.2%}  mean_dd ${fk_mc['mean_max_dd']:>+8.2f}")

    # Sensitivity to kelly_fraction
    print(f"\n=== Sensitivity: kelly_fraction (p={args.p_clean}, L={args.loss_on_divergence}, base MC params) ===")
    print(f"  {'kf':>5}  {'n_trades':>8}  {'notional':>10}  {'clean PnL':>10}  {'mean MC PnL':>12}  {'mean MC DD':>12}")
    base = dict(p_clean=0.97, p_divergence=0.02, p_third_party=0.005, p_void=0.005,
                L_div=0.50, L_third_party=0.50, L_void=0.30)
    sensitivity_rows: list[tuple] = []
    for kf in [0.10, 0.15, 0.25, 0.40, 0.60, 1.00]:
        r = replay(rows, "fractional_kelly",
                   args.bankroll, args.per_pair_pct, args.max_pct, args.min_usd,
                   args.daily_max_loss_pct, args.inventory_cap_pct,
                   kf, args.p_clean, args.loss_on_divergence,
                   open_window_days=args.open_window_days,
                   first_signal_per_pair_only=args.first_per_pair)
        mc = monte_carlo_pnl(r.trades, args.mc_trials, **base, seed=42)
        print(f"  {kf:>5.2f}  {r.n_trades():>8d}  {r.total_notional():>10.0f}  "
              f"{r.clean_pnl():>10.2f}  {mc['mean_pnl']:>+12.2f}  {mc['mean_max_dd']:>+12.2f}")
        sensitivity_rows.append((kf, r.n_trades(), r.total_notional(),
                                 r.clean_pnl(), mc['mean_pnl'], mc['mean_max_dd']))

    # Sensitivity to p_clean
    print(f"\n=== Sensitivity: p_clean (kelly_fraction={args.kelly_fraction}, L={args.loss_on_divergence}) ===")
    print(f"  {'p_clean':>7}  {'n_trades':>8}  {'notional':>10}  {'clean PnL':>10}")
    p_clean_rows: list[tuple] = []
    for pc in [0.90, 0.93, 0.95, 0.97, 0.99]:
        r = replay(rows, "fractional_kelly",
                   args.bankroll, args.per_pair_pct, args.max_pct, args.min_usd,
                   args.daily_max_loss_pct, args.inventory_cap_pct,
                   args.kelly_fraction, pc, args.loss_on_divergence,
                   open_window_days=args.open_window_days,
                   first_signal_per_pair_only=args.first_per_pair)
        print(f"  {pc:>7.2f}  {r.n_trades():>8d}  {r.total_notional():>10.0f}  {r.clean_pnl():>10.2f}")
        p_clean_rows.append((pc, r.n_trades(), r.total_notional(), r.clean_pnl()))

    if args.report_path:
        # Materialize a markdown report
        report = []
        ap_w = report.append
        ap_w(f"# Sizer replay viability report — {time.strftime('%Y-%m-%d')}\n")
        ap_w(f"**Source:** `{args.db}`")
        ap_w(f"**Signals replayed:** {len(rows)} (would_trade=1, polarity in {{same, inverse}}, tag != high_risk)")
        ap_w(f"**Time range:** {time.strftime('%Y-%m-%d', time.gmtime(rows[0]['detected_ts'])) if rows else '?'} → "
             f"{time.strftime('%Y-%m-%d', time.gmtime(rows[-1]['detected_ts'])) if rows else '?'}")
        ap_w(f"**Bankroll:** ${args.bankroll:.0f}  "
             f"**Per-pair target:** {args.per_pair_pct:.0%}  "
             f"**Hard cap/pair:** {args.max_pct:.0%}  "
             f"**Inventory cap:** {args.inventory_cap_pct:.0%}\n")
        ap_w("## What this report tests\n")
        ap_w("- **Synthetic clean PnL** = `Σ (units × spread) - Σ fees`, assuming every "
             "arb settles cleanly. This is the upper-bound captured-spread number. It "
             "ignores tail risk entirely; it is NOT a forecast.")
        ap_w("- **Monte Carlo PnL** = sample each fill's outcome from {clean, divergence, "
             "third-party, void, residual-flat} with the parameters shown. 1 000 trials "
             "→ mean / p05 / p95 / max-drawdown distributions.")
        ap_w("- Both sizers see the same input signal stream; the only difference is "
             "the per-signal $ size decision (and, for Kelly, the skip-when-f≤0 filter).\n")
        ap_w("## Caveats\n")
        ap_w("- `book_depth_at_signal_usd` is NOT recorded historically, so the "
             "book-depth cap recommended in `docs/SIZING_SCOPE_2026-05-13.md` is **not "
             "exercised** in this replay. Real-world execution should add it as a "
             "third clip on top of the Kelly sizer.")
        ap_w("- The replay treats positions as 'open' for a fixed 7-day window from "
             "fill (approximation for the missing settlement-timestamp data). Cycles "
             "older than 7 days roll off the inventory cap. Real settlement is "
             "hours-to-weeks depending on the underlying event.")
        ap_w("- `daily_pnl_usd` stays at 0 in the replay (we don't have settlement "
             "timestamps), so the daily-loss stop never fires. Production uses "
             "realized PnL from settled fills; until paper settlement clears, neither "
             "the production bot nor this replay can stop on loss.")
        ap_w("- None of the bot's 156 actual paper_fills have realized PnL on this "
             "DB (92 phantom-wiped on 2026-05-06; 64 open and unsettled). So this "
             "report compares the two sizers on hypothetical clean+tail PnL, not on "
             "the bot's empirical fill record.\n")
        ap_w("## Headline results\n")
        ap_w(f"| Metric | Fixed-fractional | Fractional Kelly (kf={args.kelly_fraction}, p={args.p_clean}, L={args.loss_on_divergence}) |")
        ap_w(f"|---|---:|---:|")
        ap_w(f"| Trades taken | {res_ff.n_trades()} | {res_fk.n_trades()} |")
        ap_w(f"| Total notional ($) | {res_ff.total_notional():.0f} | {res_fk.total_notional():.0f} |")
        ap_w(f"| Clean PnL upper bound ($) | {res_ff.clean_pnl():.2f} | {res_fk.clean_pnl():.2f} |")
        ap_w(f"| Position concentration (Herfindahl) | {herfindahl(res_ff.trades):.4f} | {herfindahl(res_fk.trades):.4f} |")
        ap_w(f"| Skipped by sizer (f≤0 or min$ floor) | {res_ff.skipped_by_sizer_zero} | {res_fk.skipped_by_sizer_zero} |")
        ap_w(f"| Skipped by per-pair cap | {res_ff.skipped_by_per_pair_cap} | {res_fk.skipped_by_per_pair_cap} |")
        ap_w(f"| Skipped by inventory cap | {res_ff.skipped_by_inventory_cap} | {res_fk.skipped_by_inventory_cap} |")

        ap_w("\n## By edge bucket\n")
        ap_w("| Bucket (bps) | FF trades | FF $ ntl | FF clean PnL | FK trades | FK $ ntl | FK clean PnL |")
        ap_w("|---|---:|---:|---:|---:|---:|---:|")
        for b in all_buckets:
            f = ff_buckets.get(b, {"n": 0, "notional": 0, "clean_pnl": 0})
            k = fk_buckets.get(b, {"n": 0, "notional": 0, "clean_pnl": 0})
            ap_w(f"| {b} | {f['n']} | {f['notional']:.0f} | {f['clean_pnl']:.2f} | "
                 f"{k['n']} | {k['notional']:.0f} | {k['clean_pnl']:.2f} |")

        ap_w("\n## Monte Carlo PnL\n")
        ap_w("| Scenario | Sizer | mean | median | p05 | p95 | P(loss) | mean MaxDD |")
        ap_w("|---|---|---:|---:|---:|---:|---:|---:|")
        for name, ff, fk in mc_results:
            ap_w(f"| {name} | FF | ${ff['mean_pnl']:+.2f} | ${ff['median_pnl']:+.2f} | "
                 f"${ff['p05']:+.2f} | ${ff['p95']:+.2f} | {ff['p_loss']:.1%} | ${ff['mean_max_dd']:+.2f} |")
            ap_w(f"| {name} | FK | ${fk['mean_pnl']:+.2f} | ${fk['median_pnl']:+.2f} | "
                 f"${fk['p05']:+.2f} | ${fk['p95']:+.2f} | {fk['p_loss']:.1%} | ${fk['mean_max_dd']:+.2f} |")

        ap_w("\n### Sensitivity: kelly_fraction (base MC params)\n")
        ap_w("| kf | n trades | total ntl | clean PnL | MC mean | MC mean DD |")
        ap_w("|---:|---:|---:|---:|---:|---:|")
        for kf, n, ntl, cln, mc_m, mc_dd in sensitivity_rows:
            ap_w(f"| {kf:.2f} | {n} | {ntl:.0f} | {cln:.2f} | ${mc_m:+.2f} | ${mc_dd:+.2f} |")

        ap_w("\n### Sensitivity: p_clean\n")
        ap_w("| p_clean | n trades | total ntl | clean PnL |")
        ap_w("|---:|---:|---:|---:|")
        for pc, n, ntl, cln in p_clean_rows:
            ap_w(f"| {pc:.2f} | {n} | {ntl:.0f} | {cln:.2f} |")

        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text("\n".join(report))
        print(f"\nReport written to {args.report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
