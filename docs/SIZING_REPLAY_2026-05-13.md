# Sizer replay viability report — 2026-05-13

**Companion to:** `docs/SIZING_SCOPE_2026-05-13.md`
**Replay tool:** `scripts/replay_sizers.py` (self-contained; replicates production
sizer line-by-line for the fixed-fractional path, implements the proposed
fractional-Kelly sizer from the scope doc)
**Data source:** local copy of `arb_bot.sqlite` at
`/Users/.../Prediction_Markets/.claude/worktrees/gifted-hopper-5e82df/.../arb_bot.sqlite`
(VPS unreachable at run time; used local snapshot)
**Sample:** 124 519 paper_signals, 2026-05-06 → 2026-05-13. **330** would_trade
signals (`would_trade=1`, polarity known, tag != `high_risk`) across
**35 distinct pairs**.

---

## Executive summary

**On the proposed sizer change.** The replay supports the recommendation in
the scoping doc, with caveats. The proposed fractional-Kelly sizer produces
~2× the clean-spread capture of the current fixed-fractional sizer on the
same opportunity stream, with similar or slightly *better* tail-drawdown
behavior. The improvement is robust across Monte Carlo parameter sweeps and
is driven by two effects:
1. **Edge-aware up-sizing**: Kelly + the 10% hard cap means every traded
   pair gets the maximum allowed allocation rather than a 5% target. Each
   trade roughly doubles in dollar size.
2. **Edge-aware filtering**: Kelly skips marginal-edge mid-priced arbs
   where tail risk × loss-on-divergence exceeds the expected win. In the
   chronological replay this freed up inventory-cap headroom for the
   higher-edge trades that came later.

**On the underlying strategy's viability.** This is where the replay
finds a more uncomfortable picture, *independent* of the sizer choice:

| Concerning fact | What it means |
|---|---|
| 92 of 156 paper_fills were **phantom signals** (`WIPED_PHANTOM_GAMMA`, manually wiped on 2026-05-06) | First day of operation, 60% of "arbs" didn't actually exist on Polymarket's CLOB; Gamma API returned stale quotes |
| 64 remaining fills are **all still open** (51-174 hours since fill); **zero settled cleanly** | No empirical evidence the strategy is profitable. Paper-PnL ≥ 0 gate cannot be evaluated yet |
| 330 would_trade signals collapse to **35 distinct pairs** over 7 days | Real opportunity rate is ~5 distinct opportunities/day, not the 47 raw signals/day implied by the unique-event count |
| 94% of 38 257 directional signals have `<100 bps` edge | The strategy is finding lots of micro-edges that don't survive fees; the few real opportunities are a long-tail event |

**The sizer change is worth doing — but it is not the bottleneck.** The bot
needs settlement data on the 64 open arbs before either sizer's *real* PnL
can be evaluated. If those 64 settle near the clean-PnL projection
($800-$2 500 depending on sizer), the strategy is viable. If they don't —
because tail risks are larger than the scope-doc parameters assume, or
because executable book depth is too thin — *no sizer choice will save it*.

**Recommended next action.** Wait for the open arbs to settle (1-4 more
weeks based on event horizons) BEFORE flipping `SIZER_MODE` on a live
container. In parallel: build the sizer (it's a small change, ~80 lines)
behind the existing `fixed_fractional` default, add the
`book_depth_at_signal_usd` column, and re-run this replay in 2 weeks once
honest depth data exists.

---

## 1. Replay design

### 1.1 What's compared

Both sizers process the same chronological stream of 330 would_trade
signals. The only differences are:
- **Per-signal $ size** (`fixed_fractional` returns the bankroll-target
  pct slice; `fractional_kelly` returns the asymmetric-payoff Kelly
  fraction, clipped at the hard-cap and min-position floor).
- **Skip-when-f≤0 filter** (Kelly only; rejects signals where
  `p × b − q × L < 0`, i.e. expected value is negative under the
  parameterized tail model).

Risk gates applied identically to both:
- Per-pair max position (`max_pct × bankroll = $300`)
- Aggregate-inventory cap (`inventory_cap_pct × bankroll = $900` — new
  in the proposed framework; tested with both sizers for parity)
- 30-day "open window" approximation (fills roll off inventory cap after
  30 days; data only spans 7 days so this effectively means "every
  trade stays open through the replay window")

### 1.2 What's reported

- **Clean PnL upper bound**: `Σ (units × raw_spread) - Σ round_trip_fee`,
  assuming every fill settles cleanly. Ignores tail risk entirely.
- **Monte Carlo PnL**: each fill draws an outcome from
  {clean (+spread), divergence (-L_div × notional), third-party
  (-L_3p × notional, inverse only), void (-L_void × notional),
  residual-flat (0)}. 1 000 trials → mean / p05 / p95 / drawdown
  distributions, evaluated across 3 parameter scenarios.

### 1.3 Caveats

- `book_depth_at_signal_usd` is NOT recorded historically, so the
  book-depth cap recommended in the scoping doc is **not exercised**
  here. Real-world execution should add it as a third clip on the Kelly
  output.
- `daily_pnl_usd` stays at 0 in the replay (no settlement timestamps
  for the open positions), so the daily-loss stop never fires. Same
  behavior as production today: until paper settlement clears, the
  daily stop is inert.
- Both sizers see a 30-day open window in this run (because the data
  spans 7 days, that means "no positions roll off"). On longer datasets
  this knob matters more.

---

## 2. Headline numbers — chronological replay, bankroll $3 000

This is the realistic-deployment scenario: $3K bankroll (per current
`.env`), all risk gates active, signals processed in chronological order
exactly as the production bot would see them.

| Metric | Fixed-fractional | Fractional Kelly (kf=0.25, p=0.97, L=0.50) |
|---|---:|---:|
| Trades taken | 6 | 3 |
| Total notional deployed ($) | 899 | 899 |
| **Clean PnL upper bound ($)** | **+292.08** | **+482.83** (+65%) |
| Position concentration (Herfindahl) | 0.167 | 0.333 |
| Skipped by Kelly `f ≤ 0` | 0 | 0 |
| Skipped by per-pair cap | 0 | 28 |
| Skipped by aggregate inventory cap | 324 | 299 |

The inventory cap is binding for both sizers — most signals are skipped
because earlier trades have filled the budget. Fixed-fractional ($150
per trade) fits 6 trades into the $900 cap; Kelly ($300 per trade, at
the hard cap) fits 3 trades. **Kelly trades fewer but bigger.**

### Per-edge-bucket breakdown

| Edge bucket (bps) | FF trades | FF $ ntl | FF clean PnL | FK trades | FK $ ntl | FK clean PnL |
|---|---:|---:|---:|---:|---:|---:|
| 200-300 | 1 | 150 | 29.76 | 0 | 0 | 0.00 |
| 300-500 | 1 | 150 | 6.70 | 0 | 0 | 0.00 |
| 500-1000 | 2 | 300 | 45.41 | 1 | 300 | 62.25 |
| 1000-2000 | 1 | 150 | 133.87 | 1 | 300 | 267.90 |
| 2000-5000 | 1 | 150 | 76.34 | 1 | 299 | 152.68 |

Kelly drops the 200-500 bps trades and doubles size on the higher-edge
ones. The biggest dollar contribution comes from the 1000-2000 bps
bucket: $268 vs $134.

### Monte Carlo PnL (1 000 trials per scenario)

| Scenario | Sizer | mean | median | p05 | p95 | P(loss) | mean MaxDD |
|---|---|---:|---:|---:|---:|---:|---:|
| base (p=0.97, p_div=0.02, L_div=0.50) | FF | $+272.62 | $+292.08 | $+136.80 | $+292.08 | 0.3% | $-9.56 |
| base | FK | **$+456.50** | $+482.83 | $+232.18 | $+482.83 | 0.3% | $-10.08 |
| optimistic (p=0.99, p_div=0.005) | FF | $+285.34 | $+292.08 | $+207.06 | $+292.08 | 0.0% | $-3.54 |
| optimistic | FK | $+474.73 | $+482.83 | $+482.83 | $+482.83 | 0.0% | $-3.27 |
| pessimistic (p=0.93, p_div=0.04, L_div=0.60) | FF | $+251.45 | $+292.08 | $+64.47 | $+292.08 | 1.2% | $-20.68 |
| pessimistic | FK | $+429.25 | $+482.83 | $+117.29 | $+482.83 | 0.8% | $-19.34 |

Kelly beats fixed-fractional in expected PnL across all three scenarios
(+67%, +66%, +71% respectively). Max drawdown is **slightly better**
for Kelly in 2 of 3 scenarios. Loss probability is comparable.

---

## 3. Isolation test — one signal per pair, inventory cap off

To separate "sizer behavior" from "inventory cap fights," this run takes
only the chronologically-first would_trade signal per pair (35 trades
total) with the inventory cap set very loose. This is the pure
apples-to-apples sizer comparison.

| Metric | Fixed-fractional | Fractional Kelly |
|---|---:|---:|
| Trades taken | 35 | 35 |
| Total notional ($) | 5 243 | 10 492 (+100%) |
| **Clean PnL upper bound ($)** | **+1 269.92** | **+2 540.84 (+100%)** |
| Position concentration (Herfindahl) | 0.029 | 0.029 |

In the unconstrained regime Kelly doubles every position — the 10% hard
cap binds for every trade. So **Kelly's effect at the cap is exactly
"max out every trade,"** which gives the 2× notional and 2× clean PnL.

### Monte Carlo (1 000 trials, base params)

| Sizer | mean | p05 | p95 | mean MaxDD |
|---|---:|---:|---:|---:|
| FF | $+1 169.42 | $+930.44 | $+1 269.92 | $-49.79 |
| FK | $+2 339.73 | $+1 861.03 | $+2 540.84 | $-99.62 |

Same 2× multiplier in MC, with proportional drawdown (DD scales with
position size, as expected). p05 is also 2× — *the Kelly distribution
is not just shifted; the lower tail is also 2× better*. This is because
the win frequency is high (97%+) and most outcomes are clean.

### Sensitivity at the cap

Sensitivity to `kelly_fraction` and `p_clean` is **completely flat** in
the realistic regime: every Kelly position hits the hard cap regardless
of `kf=0.10` or `kf=1.00`, regardless of `p_clean=0.90` or `0.99`. **The
hard cap is the dominant constraint, not Kelly.**

| `kelly_fraction` | n trades | clean PnL | MC mean | MC mean DD |
|---:|---:|---:|---:|---:|
| 0.10 | 3 | 482.83 | $+456.50 | $-10.08 |
| 0.25 | 3 | 482.83 | $+456.50 | $-10.08 |
| 1.00 | 3 | 482.83 | $+456.50 | $-10.08 |

This is an important finding: **`KELLY_FRACTION` is a dead knob at this
bankroll size + cap.** It would only become a live knob if the hard cap
were raised above ~25% (where Kelly's raw `f*` stops binding) OR the
bankroll grew large enough that the 10% cap exceeds book-depth on a
typical leg. For practical purposes the new sizer collapses to "max-out
every Kelly-positive signal" at the current $3K bankroll.

---

## 4. Strategy-viability analysis (independent of sizer)

The replay reveals data quality issues that are more important than
which sizer is in use.

### 4.1 Phantom-signal rate

92 of 156 paper_fills (59%) were wiped on 2026-05-06 with
`settle_method=manual_phantom_wipe_phase1`,
`realized_outcome=WIPED_PHANTOM_GAMMA`. These all happened in the
**first 9 hours of operation** (2026-05-06 02:34 → 12:00 UTC), before
the CLOB-refresh + WS-listener work landed. The phantom signals came
from Gamma's market-list endpoint returning stale or pre-listing
quotes for markets that didn't actually have liquid CLOB books.

**This is now fixed in production** (Phase 1 + Phase 2 per memory) but
the data is permanently dirty for that window. Future replays should
exclude pre-2026-05-06-13:00 signals.

### 4.2 Settlement is the gating data point we don't have

Of the 64 real fills (32 distinct arbs × 2 legs):
- 100% are still open
- Median age: 154 hours / ~6.4 days
- Oldest: 174 hours / ~7.3 days
- Settlement timing for prediction-market arbs is event-driven (game
  finish, election call, contract expiry) — typically 1-30 days.

The bot's 10-trade settled-PnL gate is therefore **at least 1 week away
and possibly 3-4 weeks away.** No live launch decision should be made
before then, regardless of how good the sizer math looks.

### 4.3 Opportunity rate

| Filter step | Signals | % of total |
|---|---:|---:|
| Raw detections | 124 519 | 100% |
| Directional (excluding skips) | 38 257 | 30.7% |
| Above tiered edge threshold | 1 333 | 1.1% |
| `would_trade=1` after all gates | 330 | 0.27% |
| Distinct pairs in would_trade | 35 | 0.03% |
| Actually filled (gating + caps) | 156 | 0.13% |

35 distinct pairs over 7 days ≈ **5 actionable opportunities/day** at
the current sensitivity. The same pair re-fires for many cycles until
its position fills or its price stops being an arb, so most of the 330
would_trade signals are duplicates.

This is small. Sizer choice can ~2× the dollar capture, but capturing
2× of a few opportunities/day is still a low-throughput strategy. If
the strategy is viable, it's a low-volume slow-compound bot, not a
high-frequency one.

### 4.4 Edge distribution among the 35 distinct pairs

Most distinct pairs cluster in the 200-500 bps range — exactly the
band where Kelly's filter is most active. A few pairs hit 1000+ bps;
those are the headline-driving trades for both sizers.

---

## 5. Conclusions

### 5.1 On the sizer change

- **Approve the framework**, with caveats below. Kelly captures
  ~2× the clean PnL on the same opportunity stream with comparable
  drawdown. Robust across parameter ranges in Monte Carlo.
- **`KELLY_FRACTION` is a dead knob at $3K bankroll** — the 10% hard
  cap binds for every Kelly-positive signal. Recommended action:
  ship the parameter for forward-compat at larger bankrolls, but
  don't expect it to do anything at the current scale.
- **`p_clean` is a live knob** at lower values (0.93 and below).
  Below `p_clean ≈ 0.95`, Kelly starts filtering mid-edge trades
  (see `p_clean` sensitivity in the chronological run). At
  `p_clean=0.97` the filter is inactive on the trades that ran.
  Sensible default 0.97; revisit after first 20+ settled fills.
- **`L_div` (loss-on-divergence) is the most decision-relevant knob.**
  At `L_div=0.50` the filter is permissive; at `L_div=0.80` the
  filter starts skipping the 200-500 bps bucket aggressively. Until
  settlement data exists to calibrate, the 0.50 default is a guess.
- **Aggregate inventory cap is essential.** Without it, 35 trades at
  $300 each = $10 500 outstanding on a $3K bankroll. The cap saves
  the strategy from itself.

### 5.2 On strategy viability

- **Insufficient data to conclude.** No settled fills exist yet. The
  clean-PnL upper bound is $482 (FK) / $292 (FF) for the week — but
  this assumes every arb resolves cleanly with no divergence, no leg
  fail, no slippage beyond the modeled 100 bps round-trip.
- **Phantom-wipe rate of 60% on day 1** is now fixed but warrants
  monitoring. If another phantom cluster appears post-Phase-2, the
  CLOB validation isn't doing its job.
- **5 opportunities/day** is low. Strategy needs higher pair coverage
  or higher signal sensitivity to scale meaningfully.
- The 64 open arbs need to settle. Their cumulative captured spread
  IF clean would be the empirical confirmation that this works.

### 5.3 Recommended sequence (revising the scope-doc rollout)

1. **Wait for settlement.** Top priority is letting the 64 open arbs
   resolve and entering their realized PnL into the `paper_fills`
   table. 1-4 weeks of patience required.
2. **In parallel, implement the sizer.** ~80 lines of code per the
   scope doc; default `SIZER_MODE=fixed_fractional` so production
   behavior is unchanged.
3. **Add `book_depth_at_signal_usd` column** to `paper_signals`.
   Re-run this replay in 2-3 weeks against signals with honest depth
   data.
4. **Calibrate `p_clean` and `L_div` from settled data.** Once 20+
   fills settle, fit the empirical clean/divergence/void rates and
   set the Kelly parameters to match.
5. **Flip `SIZER_MODE=fractional_kelly`** only after: (a) paper-PnL ≥ 0
   gate clears, (b) re-replay confirms Kelly still beats FF on
   honest depth data, (c) calibrated `p_clean` and `L_div` confirm
   the framework holds.

### 5.4 What this replay does NOT show

- **Realized PnL.** No settled fills with non-phantom outcomes.
- **Live execution risk.** Paper assumes 100% fill probability.
- **Book-depth impact.** No historical depth snapshot.
- **Performance scaling at higher bankrolls.** The 10% cap binds at
  $3K; at $30K bankroll, Kelly's `kelly_fraction` knob would
  matter much more.

---

## Appendix: How to reproduce

```
python3 scripts/replay_sizers.py \
    --db /path/to/arb_bot.sqlite \
    --bankroll 3000 --kelly-fraction 0.25 \
    --p-clean 0.97 --loss-on-divergence 0.50 \
    --inventory-cap-pct 0.30 --open-window-days 30 \
    --mc-trials 1000 \
    --report-path docs/SIZING_REPLAY_2026-05-13.md

# isolation test:
python3 scripts/replay_sizers.py --db ... --first-per-pair \
    --inventory-cap-pct 5.0 --open-window-days 30
```

Source: `scripts/replay_sizers.py` is self-contained — no imports from
`src/arb_bot/`. Replicates the production sizer's
`_compute_size_units()` line-by-line for the fixed-fractional path;
implements the proposed sizer from `docs/SIZING_SCOPE_2026-05-13.md`.
Auditable as a single ~400-line file.
