# Arb_Bot sizing scope — does Kelly even apply, and if so, how

**Date:** 2026-05-13
**Status:** Scoping only. No code changes in this doc. No implementation until the
user reviews and authorizes.
**Bot state:** Bot #12 LIVE (paper) since 2026-05-05. Paper-PnL ≥ 0 gate for live
launch has NOT cleared. This sizing change is gated behind that same paper-PnL
criterion — it does not bypass it.
**Repo:** `M0byNick/Predictions_Market_Trading_Bot` @ `c63d701` (PR #8, "tighter
cap + tiered edge + $3K bankroll" merged 2026-05-12).

---

## 1. Current sizing audit

### 1.1 Where the code lives

| Concern | File | Function / lines |
|---|---|---|
| $-target → contract-unit conversion | `src/arb_bot/signal/spread.py` | `_compute_size_units()` lines 179-194 |
| Capital required per contract | `src/arb_bot/signal/spread.py` | `_capital_per_unit()` lines 164-176 |
| Call site that wires sizing into the signal | `src/arb_bot/signal/spread.py` | `detect_for_pair()` lines 393-396 |
| Bankroll-derived dollar limits | `src/arb_bot/config.py` | `Config.paper_*` properties, lines 95-118 |
| Pre-fill risk gates | `src/arb_bot/risk/limits.py` | `check()` lines 51-59 |
| Paper-fill recording | `src/arb_bot/executor/paper.py` | `simulate_fill()` lines 28-109 |
| Per-leg PnL math at settlement | `scripts/settle_paper_fills.py` | `_pnl_for_leg()` lines 70-82 |

### 1.2 What it does today

**Fixed-fractional, bankroll-driven.** No edge-magnitude scaling at all.

User config (`.env.example` and `config.py:140-172`):

```
INITIAL_BANKROLL_USD=3000
PAPER_MAX_POSITION_PCT=0.10          → hard cap $300 per pair
PAPER_PER_PAIR_TARGET_PCT=0.05       → target $150 per arb position
PAPER_MIN_POSITION_USD=25
PAPER_DAILY_MAX_LOSS_PCT=0.05        → daily stop at $150
PAPER_MIN_EDGE_BPS=200               → base edge gate (tiered down for short-dated)
SLIPPAGE_BPS_PER_LEG=50              → 100 bps round-trip in signal gating
MAX_DAYS_TO_RESOLVE=30               → capital-efficiency hard reject
```

Algorithm per signal (`signal/spread.py:393-396`):

```
target_usd        = clip(bankroll × per_pair_target_pct,
                         floor = min_position_usd,
                         ceil  = max_position_usd)
capital_per_unit  = max(kal_mid, poly_mid)          if same polarity
                  = kal_mid + poly_mid              if inverse polarity
size_units        = max(1, int(target_usd / capital_per_unit))
```

`target_usd` is **identical for every signal** — it depends only on bankroll
config, not on this signal's edge, P(clean execution), or book depth.
`capital_per_unit` depends only on the polarity and mid prices, not on the edge
either. Two signals with the same polarity and similar mids get the same number
of units regardless of whether the spread is 200 bps or 2000 bps.

### 1.3 Edge handling — gate, not sizer

The bot has *tiered* edge floors (`spread.py:120-148`, `_tier_min_edge_bps()`):

```
≤  3d → 40% × base   (e.g. 80 bps)
4-7d  → 60% × base   (120 bps)
8-14d → 85% × base   (170 bps)
15+d  → 100% × base  (200 bps)
```

Edge is also used to compute an `annualized_edge_bps` capital-efficiency display
metric (`spread.py:421-425`). But the **sizing function never reads the edge**.
Below threshold → reject. Above threshold → fixed slice. Binary.

### 1.4 Inventory / portfolio handling

`risk/limits.py:check()` enforces two cross-time gates per pair:

1. **Daily PnL stop** (cross-pair): daily realized PnL ≥ -$150 → halt all new entries.
2. **Per-pair open position cap**: open notional on this pair < $300 → block re-entry.

**Missing:** No aggregate-across-pairs inventory cap. If 20 pairs all trip
simultaneously the bot will open 20 × ~$150 = $3,000 of notional = 100% of
bankroll. The only catch is that signals are processed sequentially in
`main.py:38-47`, so the daily-loss cap could fire mid-cycle if early fills lose
fast — but if no losses settle within the cycle, nothing slows the bot down.

### 1.5 Liquidity awareness — present but unused for sizing

`signal/validate.py:_fetch_poly_quote_via_clob()` (lines 109-160) already returns
`poly_book_size_usd` (sum of resting $ at best bid + best ask). The dashboard
uses it. The signal gate `is_arb_now` uses it for a "THIN book" warning string
(`validate.py:319-320`). **It is not fed into sizing.** A 10 000-share arb on a
$50-deep Polymarket book gets the same size as one on a $5 000-deep book.

### 1.6 Slippage model

There are two distinct slippage numbers in the code and they don't agree:

- **Signal gating** (`signal/spread.py:151-161`): `_round_trip_slippage_bps =
  2 × cfg.slippage_bps_per_leg = 100 bps` (with default `SLIPPAGE_BPS_PER_LEG=50`).
- **Paper-fill simulation** (`executor/paper.py:11`): `SLIPPAGE_BPS = 30` per
  leg (hardcoded, not from config). Applied symmetrically: buyer fills at
  `mid + 30bps`, seller at `mid - 30bps`.

That's 60 bps round-trip simulated vs. 100 bps round-trip required for the gate
to fire. The gate is conservative relative to the paper fill, so paper PnL is
biased slightly favorable. **Not in scope for this change** — flag it as a
follow-up. The right number is whatever holds up against live data.

### 1.7 Settlement model

`scripts/settle_paper_fills.py:_pnl_for_leg()` (lines 70-82) computes per-leg
realized PnL conditional on a binary outcome (`yes` or `no`). For a buy:
`+ N × (1 - P) - F` if YES, `- N × P - F` if NO. Settlement is heuristic:
"market closed AND mid ≥ 0.97" → YES; "≤ 0.03" → NO; ambiguous mids stay
pending. This is paper-only; live settlement should use venue resolution APIs.

For the sizing analysis below, the relevant property is this: **per-leg PnL is
binary and bounded** (`-N × P - F` on a worthless buy, etc.). So per-position
PnL is the sum of two binary legs, which makes the loss-on-divergence number
tractable.

---

## 2. Kelly-applicability analysis

### 2.1 Why this question is non-trivial

Cross-venue arb is often described as "risk-free" because two equal and opposite
legs settle against each other. In the idealized risk-free case Kelly degenerates:
you'd allocate as much as possible subject to capital constraints, because there
is no `p < 1` to penalize size. The sizer is then a constrained linear program
("max Σ spread_i × size_i subject to Σ size_i ≤ bankroll and size_i ≤ depth_i"),
which is a different mathematical object than Kelly.

But cross-venue prediction-market arb is **not** truly risk-free. It has at
least seven distinct residual risks (enumerated below), each of which converts
the "risk-free" deterministic payoff into a small random payoff with a fat-left
tail. Those tails make Kelly the right framework — but not the textbook
coin-flip Kelly. We need a Kelly variant that handles a high-`p`, low-`b`,
catastrophic-tail payoff distribution. That's the analysis below.

### 2.2 Per-trade payoff model

For a single arb position with stake `S` (dollars of capital committed),
captured spread `s` (fraction of stake, after fees + slippage), define five
discrete outcomes:

| Outcome | Probability | Per-$1-stake payoff |
|---|---:|---|
| Clean settlement (both legs as expected) | `p_clean` | `+s` |
| One-leg execution fail (naked leg, random direction) | `p_leg_fail` | `≈ 0` (expectation; var is large) |
| Resolution divergence (legs settle inconsistently) | `p_divergence` | `≈ -L_div`, with `L_div ∈ [0.3, 1.0]` |
| Inverse-polarity third-party tail (both NO) | `p_third_party` | `≈ -L_3p`, with `L_3p ∈ [0.5, 1.0]` |
| Void / push on one venue, settle on other | `p_void` | `≈ -L_void`, with `L_void ∈ [0.2, 0.5]` |

with `p_clean + p_leg_fail + p_divergence + p_third_party + p_void = 1`.

Order of magnitude for the live universe (informed guess; needs paper-mode
calibration):
- `p_clean` ≈ 0.95–0.99 (most arbs settle cleanly)
- `p_leg_fail` ≈ 0.005–0.02 (mitigated significantly by atomic-execution work
  planned pre-live; pre-mitigation it could be much higher on volatile pairs)
- `p_divergence` ≈ 0.005–0.03 (depends on the LLM adjudicator's quality and how
  aggressively we trust `resolution_divergence_risk` labels)
- `p_third_party` ≈ 0.001–0.01 on inverse-polarity pairs (zero on same-polarity)
- `p_void` ≈ 0.001–0.01 (game cancellations, etc.)

Effective per-$1 EV:

```
E[payoff/$]  =  p_clean × s
              - p_divergence × L_div
              - p_third_party × L_3p
              - p_void × L_void
              - p_leg_fail × (small + variance)
```

Effective `b` (size of win) and `q` (probability of loss × loss magnitude):
- `b ≈ s` (small, e.g. 0.02–0.05)
- `q ≈ p_divergence × L_div + p_third_party × L_3p + p_void × L_void`
  (small, e.g. 0.005–0.03)

### 2.3 Kelly with this payoff distribution

The continuous-payoff Kelly criterion solves
`f* = argmax_f E[log(1 + f × payoff/$)]`.

For a simple binary approximation (clean OR divergence, ignoring leg-fail and
third-party for the first-cut sizer):
- Win `b = s` with prob `p_w = p_clean`
- Lose `L = L_div` with prob `p_L = p_divergence`

Two-state Kelly with asymmetric win/loss:
```
f* = (p_w × b - p_L × L) / (b × L)
```

Worked numbers:

| `p_clean` | `s` (win) | `p_divergence` | `L_div` | `f*` (full Kelly) | Quarter-Kelly (×0.25) |
|---:|---:|---:|---:|---:|---:|
| 0.97 | 0.02 | 0.02 | 0.50 | `(0.97×0.02 − 0.02×0.50)/(0.02×0.50) = 0.94` | 0.235 |
| 0.95 | 0.02 | 0.04 | 0.50 | `(0.95×0.02 − 0.04×0.50)/(0.02×0.50) = -0.10` | **NEGATIVE — skip** |
| 0.99 | 0.05 | 0.01 | 0.50 | `(0.99×0.05 − 0.01×0.50)/(0.05×0.50) = 1.78` | 0.445 |
| 0.97 | 0.005 | 0.005 | 0.50 | `(0.97×0.005 − 0.005×0.50)/(0.005×0.50) = 0.94` | 0.235 |

Three things to notice:

1. **Tiny spreads with material tail probability go negative.** Row 2 — a
   200 bps spread (`s = 0.02`) on a pair where divergence probability is 4%
   and loss-on-divergence is 50% has *negative* Kelly. The sizer correctly
   says "don't bet." This is also exactly why the current `paper_min_edge_bps`
   gate works as a safety filter, but the right framework lets the gate
   emerge from the parameters rather than being hand-set.

2. **Fractional Kelly is essential.** Full Kelly in row 1 says "stake 94% of
   bankroll on this one arb." That is obviously wrong, both because the
   parameter estimates are uncertain (we don't actually know `p_clean`) and
   because positions are concurrent + correlated (see §2.4). Quarter Kelly
   ≈ 23.5% of bankroll on one pair is still aggressive and the hard cap
   (currently 10%) binds.

3. **Kelly rewards higher edges.** Row 3 vs row 1: doubling the spread from
   2% to 5% and halving divergence probability lifts `f*` from 0.94 to 1.78
   (capped by the hard cap regardless). The current fixed-fractional sizer
   gives them both the same 5% slice. That is the central efficiency loss.

### 2.4 Why bets are not independent over time

Kelly assumes i.i.d. payoffs. Arb_Bot's payoffs are NOT i.i.d.:

- **Open positions are concurrent**, not sequential. Many pairs trip during
  the same cycle. Until they settle (hours to weeks), capital is locked in
  multiple correlated positions simultaneously.
- **Divergence risk is partly venue-systemic.** If Polymarket's UMA oracle has
  a bad week (mass dispute window, infrastructure outage), ALL open Poly legs
  face elevated divergence risk together — not 10 independent draws.
- **Leg-execution failure spikes are time-correlated** (volatile pre-event
  windows, big news drops). One latency event can blow out several open
  signals' execution.

Two practical implications:

(a) The Kelly fraction needs to be **divided across concurrent positions**,
not applied independently to each. A simple way: cap aggregate open notional
(across all pairs) at some fraction of bankroll, so the Kelly-per-pair sizer
operates inside a portfolio-budget envelope.

(b) The `kelly_fraction` knob should be **at most ¼ Kelly**, not the
textbook ½. With high parameter uncertainty + correlated concurrent positions,
¼ Kelly is the practical literature default (Thorp, MacLean–Thorp–Ziemba,
Vince).

### 2.5 Conclusion: Kelly applies, with three modifications

1. **Bounded fractional Kelly** (`f_actual = clip(¼ × f_Kelly, 0, hard_cap)`).
2. **Aggregate-inventory budget** across concurrent open positions
   (independent of per-position cap).
3. **Edge-floor gate** preserved (refuse trades where the parameters make
   `f_Kelly ≤ 0` — which is what `paper_min_edge_bps` already approximates).
4. **Book-depth cap** stacked on top (the sizer's output is also clipped at
   some fraction of the thinner-side top-of-book, so it can't eat through
   the spread it's trying to capture).

Kelly does NOT degenerate to "max position" because the residual risks are
real and tractable. Kelly is also NOT the textbook Kelly because of parameter
uncertainty + correlation. The right shape is fractional Kelly with explicit
risk parameters and outer hard caps.

---

## 3. Recommended sizer + justification

### 3.1 Recommendation

**Bounded fractional-Kelly sizer with explicit risk parameters, book-depth
cap, and a portfolio inventory cap.** Specifically, replace the current
`_compute_size_units()` body with this logic (pseudocode, not final code):

```
def _compute_size_units(cfg, polarity, kal_mid, poly_mid,
                       edge_bps, book_depth_usd):
    if cfg.sizer_mode == "fixed_fractional":
        return _compute_size_units_fixed(...)           # current path

    # --- fractional Kelly path ---
    s = max(edge_bps / 10_000, 0.0)                     # spread as fraction of stake
    p = cfg.kelly_p_clean
    L = cfg.kelly_loss_on_divergence
    q = 1.0 - p

    # asymmetric-payoff Kelly; clamp at 0 (negative → skip)
    f_kelly = (p * s - q * L) / max(s * L, 1e-9)
    f_kelly = max(f_kelly, 0.0)
    f_target = f_kelly * cfg.kelly_fraction             # ¼ Kelly default

    # bankroll → target $; clipped to outer hard caps
    target_usd = f_target * cfg.initial_bankroll_usd
    target_usd = min(target_usd, cfg.paper_max_position_usd)

    # book-depth cap (thinner-side depth × cap fraction)
    if book_depth_usd is not None:
        target_usd = min(target_usd, book_depth_usd * cfg.book_depth_fraction_cap)

    if target_usd < cfg.paper_min_position_usd:
        return 0, 0.0

    cpu = _capital_per_unit(polarity, kal_mid, poly_mid)
    units = max(1, int(target_usd / cpu))
    return units, units * cpu
```

Aggregate-inventory cap goes into `risk/limits.py:check()`, evaluated **before**
the per-pair check:

```
total_open = sum_open_notional_across_all_pairs(conn)
if total_open + planned_notional > cfg.concurrent_inventory_cap_pct * cfg.initial_bankroll_usd:
    return False, f"would exceed aggregate inventory cap"
```

### 3.2 Why this beats the alternatives

**vs. status quo (fixed-fractional):**
The current sizer is edge-blind. Two signals with identical mids and polarity
get identical sizes whether the edge is 200 bps or 1 500 bps. Under Kelly the
latter deserves several times more capital because the win-to-tail-loss ratio
is dramatically more favorable. The current sizer also has no book-depth or
aggregate-inventory awareness, so it can under-cover a great opportunity *and*
over-extend in a busy hour.

**vs. constrained linear program ("treat as risk-free, max Σ spread×size"):**
This is the right framework if and only if the arb is genuinely risk-free.
It is not. The LP would happily allocate full bankroll across the top-N
opportunities, ignoring the divergence + leg-fail tail that occasionally
returns -50% of notional. On day 1 the LP looks great; on day 30 a single
correlated bad event eats six months of gains. Also, the LP requires a
synchronous view of all opportunities and online optimization across them,
which the current cycle structure doesn't provide and would add real
complexity. Kelly-per-opportunity + portfolio cap captures most of the LP's
benefit (allocate more to better opportunities) without the machinery.

**vs. full Kelly:**
Full Kelly is fragile to parameter error. We don't know `p_clean` to better
than ±2 percentage points until ~hundreds of settled paper trades exist.
A 2pp error in `p_clean` can flip `f*` from +0.94 to negative (cf. §2.3
row 1 vs row 2). Quarter Kelly tolerates parameter error and still beats
fixed-fractional on growth.

**vs. naïve half-Kelly:**
Half-Kelly is the textbook safe default for *independent* bets. Concurrent +
correlated bets push the practical sweet spot lower. Quarter Kelly is the
common pragmatic choice in portfolio-Kelly literature when correlation is
present and uncertain.

**vs. pure book-depth cap (without Kelly):**
Necessary but not sufficient. A depth-cap-only sizer would still scale every
opportunity to the same depth-adjusted dollar amount regardless of edge.
Kelly + depth cap is strictly better than depth cap alone (when Kelly says
"smaller", take the smaller; when depth says "smaller", take the smaller; the
min of both is at least as good as either alone).

### 3.3 What stays the same

- All current safety gates remain (`paper_max_position_pct` hard cap,
  `paper_min_position_usd` floor, `paper_daily_max_loss_pct` daily stop,
  `max_days_to_resolve` capital-efficiency reject, tiered `paper_min_edge_bps`
  threshold).
- The tiered edge floor stays as a *gate*, separately from the Kelly sizer.
  Kelly's "f ≤ 0 → skip" is conceptually the same thing, but the explicit
  bps floor protects against parameter mis-estimation (you don't want
  optimistic `p_clean` letting in micro-edge trades whose Kelly slipped
  positive by rounding).
- Polarity-aware `_capital_per_unit()` unchanged.
- Signal-detection logic (polarity branching, fee model, stale-quote guard,
  resolution-time cap) unchanged.
- Settlement logic (`settle_paper_fills.py`) unchanged.

---

## 4. Implementation scope

**This section describes WHERE the change goes. It does NOT implement.**

### 4.1 New config fields (`src/arb_bot/config.py`)

Add to `Config` dataclass + `load_config()`:

| Env var | Default | Range | Purpose |
|---|---:|---|---|
| `SIZER_MODE` | `fixed_fractional` | `fixed_fractional` \| `fractional_kelly` | feature flag; default keeps current behavior |
| `KELLY_FRACTION` | `0.25` | `[0.05, 1.0]` | how much of Kelly to deploy; quarter-Kelly default |
| `KELLY_P_CLEAN` | `0.97` | `[0.80, 0.999]` | probability both legs execute + settle cleanly |
| `KELLY_LOSS_ON_DIVERGENCE` | `0.50` | `[0.10, 1.0]` | fraction of stake lost when an arb breaks |
| `BOOK_DEPTH_FRACTION_CAP` | `0.25` | `[0.05, 1.0]` | max fraction of thinner-side top-of-book to consume |
| `CONCURRENT_INVENTORY_CAP_PCT` | `0.30` | `[0.05, 1.0]` | aggregate open notional cap, as fraction of bankroll |

All existing fields are preserved. Defaults are conservative — most
configurations land identical or smaller positions than today's
fixed-fractional sizer, never larger before the hard cap binds.

### 4.2 Code surfaces touched

| File | Change |
|---|---|
| `src/arb_bot/config.py` | Add 6 new fields + env wiring. No existing field renamed or removed. |
| `src/arb_bot/signal/spread.py` | Extend `_compute_size_units()` signature to accept `edge_bps` and `book_depth_usd`. Branch on `cfg.sizer_mode`. Existing `fixed_fractional` path lifted into a helper and called when flag is unset. |
| `src/arb_bot/signal/spread.py` | `detect_for_pair()` passes the now-known `edge_bps` and (optionally) a freshly-fetched `book_depth_usd` into `_compute_size_units()`. Today the call happens *after* edge is computed (line 394), so this is a trivial parameter pass. Book depth source: prefer `markets.liquidity` (already in DB) for speed; fall back to None when missing. |
| `src/arb_bot/risk/limits.py` | Add `aggregate_open_position_usd(conn)` helper and extend `check()` to enforce `CONCURRENT_INVENTORY_CAP_PCT`. |
| `.env.example` | Document the six new env vars next to the bankroll block. |
| `src/arb_bot/dashboard/pnl.py` | Show aggregate-open-notional gauge alongside the daily-PnL gauge. (Nice-to-have, can defer.) |

### 4.3 Explicitly out of scope

- Signal-detection logic (no changes to `_detect_same_polarity`,
  `_detect_inverse_polarity`, `_tier_min_edge_bps`).
- Fee model (`_kalshi_taker_fee_per_contract`, `_poly_taker_fee_per_contract`).
- Polarity / mapping / adjudication.
- Paper-fill simulator's slippage model (the 30 vs 50 mismatch noted in
  §1.6 is logged as a follow-up).
- Settlement heuristic.
- Live-execution wiring (atomic 2-leg, wallet integration, etc.).

### 4.4 Backward compatibility

`SIZER_MODE=fixed_fractional` is the default in both the dataclass and the
`.env.example`. A user who upgrades the bot without touching their `.env` sees
zero behavior change. The new Kelly path activates only when
`SIZER_MODE=fractional_kelly` is set, AFTER paper validation completes.

---

## 5. Test plan + side-by-side comparison methodology

### 5.1 Comparison design

Same input stream → two sizers → compare PnL, drawdown, position concentration.
**This is a mechanical comparison, not a statistical test.** With the same
opportunity stream and deterministic settlement, the only difference between
runs is the sizer's output. PnL difference = sizer effect.

Two ways to do it (both viable; prefer Option A if simple):

**Option A — Replay log.** Add `scripts/replay_signals.py`:
1. Read every `paper_signals` row with `would_trade=1` from the production
   paper DB.
2. For each signal, re-evaluate the sizer (current vs new) using the stored
   prices, polarity, and edge.
3. Build two parallel synthetic fill streams in throwaway tables
   `paper_fills_v1` (current) and `paper_fills_v2` (new).
4. Run `settle_paper_fills.py`-equivalent settlement against both.
5. Emit a comparison report (CSV/markdown): cumulative PnL, max drawdown,
   trade count, position concentration (Herfindahl on outstanding
   notional), # of skip-due-to-cap.

**Option B — Parallel paper containers.** Run two bot instances side-by-side
with separate DB paths, same Kalshi/Poly polling. More expensive (2× API
calls, 2× container footprint) and the two won't see *exactly* the same
opportunity stream because of polling jitter. Use only if Option A turns out
infeasible (e.g., the paper_signals table doesn't have enough information
to deterministically replay the sizer).

### 5.2 Inputs to the replay

For each signal row we need: `kalshi_yes_mid`, `poly_yes_mid`,
`fee_adjusted_edge_bps`, `direction`, `polarity` (joined from
`approved_pairs`), `pair_id`, `detected_ts`. The `paper_signals` schema
(`db.py:106-119`) already stores all of these. Book-depth at signal time
is *not* stored historically — for the replay we can either:

- Use the latest `markets.liquidity` as a proxy (rough; underestimates the
  cap effect because depth was already wider at signal time on average), or
- Add a `book_depth_at_signal_usd` column going forward and run the replay
  only on signals after the column lands.

Pick the column-add path; it's a 5-line ALTER and lets the replay be honest.

### 5.3 Outputs

A markdown report at `docs/SIZING_REPLAY_<date>.md` with:

| Metric | Fixed-fractional (current) | Fractional Kelly (new) |
|---|---|---|
| Trades opened | n | n |
| Trades skipped by inventory cap | n | n |
| Total notional traded ($) | $X | $X |
| Cumulative realized PnL ($) | $X | $X |
| Cumulative realized PnL (bps of bankroll) | bp | bp |
| Max drawdown ($) | $X | $X |
| Position concentration (Herfindahl of outstanding $) | h | h |
| Median position size ($) | $X | $X |
| 90th-pct position size ($) | $X | $X |

Plus equity-curve overlay PNG / SVG.

### 5.4 Pass criteria

The new sizer should:
1. **Maintain or improve cumulative paper PnL** vs current sizer over the
   same window.
2. **Not increase max drawdown by more than 50%** of the current sizer's
   max drawdown.
3. **Reduce or hold position concentration** (Herfindahl no higher than
   current).
4. **Skip more low-edge trades** (Kelly's `f ≤ 0` filter activates on
   marginal opportunities — this is *expected* and desirable).

If criteria 1+2 pass, the new sizer is a clean win. If 1 passes but 2 fails,
tighten `KELLY_FRACTION` to 0.15 or 0.10 and re-run. If neither passes,
keep `fixed_fractional` for live and iterate parameters with new data.

### 5.5 Test coverage to add

- Unit tests in `tests/test_sizer.py` (new file) covering: full-Kelly
  formula on the worked-example payoff table in §2.3; clipping at 0 when
  `f_kelly ≤ 0`; clipping at `paper_max_position_pct`; book-depth clip
  binding before bankroll clip; `fixed_fractional` path produces
  byte-identical output to today's code.
- Integration test: `_compute_size_units(SIZER_MODE=fractional_kelly,
  edge_bps=200, bankroll=$3000, defaults)` returns expected size.
- Risk test: aggregate-inventory cap correctly blocks the (N+1)-th signal
  when N signals' total notional already reaches the cap.

### 5.6 Compute footprint

The replay is small. ~9 420 paper signals to date (per memory), each is
arithmetic + one SQLite write — total runtime is seconds to a minute on
the dev box. **No long-running compute job** triggered by this plan, so
no pre-launch compute-planning discussion needed beyond the inline check
("does the replay fit in <1 min on the laptop?" — yes).

---

## 6. Deployment gating + rollout plan

### 6.1 Gating

The bot's existing **paper PnL ≥ 0 gate for live launch is unchanged and
takes precedence over this sizer change.** Sequence:

```
[ paper PnL ≥ 0 over ≥ 10 settled fills ]   ← live-launch gate (unchanged)
              │
              └── independent of sizer choice
[ new sizer matches or beats current sizer on replay ]   ← sizer-change gate
              │
              └── gates the sizer flip to live, not the live-launch decision
```

Two independent gates. Both must pass before flipping a live bot to
`SIZER_MODE=fractional_kelly`. The bot is currently 5 paper trades into the
10-trade settled-PnL window (per memory). The sizer change can be developed,
unit-tested, and replay-validated **in parallel** with the remaining paper
window — no need to wait for live readiness before scoping the sizer.

### 6.2 Rollout sequence

1. **Implement** — code lands in a feature branch `kelly-sizer` with
   default `SIZER_MODE=fixed_fractional`. Unit tests + replay tests pass.
   PR review against `main`.
2. **Add book-depth-at-signal column + 2 weeks of new signals.** The
   replay needs honest book depth; backfill is impossible, so we wait for
   the column to fill.
3. **Run replay** on the new-column window. Emit
   `docs/SIZING_REPLAY_<date>.md`.
4. **Review replay report** with the user before flipping `SIZER_MODE` on
   any live container.
5. **Cutover** to `SIZER_MODE=fractional_kelly` (still paper) for 1 week
   minimum. Watch dashboard for surprises (aggregate-inventory cap firing
   unexpectedly, position-concentration anomalies, etc.).
6. **Live launch** only after both gates have passed AND the user has
   given explicit authorization. The live-launch checklist (pre-existing:
   Kalshi sandbox orders, Polymarket Mumbai testnet, atomic 2-leg
   execution, ≥10 settled paper PnL ≥ 0) is unchanged.

### 6.3 Rollback plan

The flag is the rollback. `SIZER_MODE=fixed_fractional` reverts behavior
without a code deploy. The `fixed_fractional` path stays in the codebase
for at least one full paper-PnL window after the cutover.

### 6.4 What the user should authorize before §6.1 step 1

Confirm:
- The recommended sizer (bounded fractional Kelly) is acceptable in
  principle.
- The default parameters (`KELLY_FRACTION=0.25`, `KELLY_P_CLEAN=0.97`,
  `KELLY_LOSS_ON_DIVERGENCE=0.50`) are reasonable starting points.
- The aggregate-inventory cap default of 30% of bankroll is reasonable.
- The replay-based comparison methodology is what the user wants (vs
  parallel containers).
- The 2-week wait for honest book-depth data is acceptable, OR the user
  wants to proceed with `markets.liquidity` as an imperfect proxy
  immediately.

---

## Appendix A — Audit gap noted but out of scope

- **Slippage model inconsistency** (§1.6): signal gate uses 100 bps
  round-trip; paper executor uses 60 bps round-trip. Bias is conservative
  on the gate, slightly favorable on the fill. Should be reconciled
  against the first batch of live fills, but not in this sizer change.
- **No signal-time book-depth snapshot.** Add a
  `book_depth_at_signal_usd` column to `paper_signals` for honest replay.
  Trivial schema migration.
- **Paper-mode `FILL_PROBABILITY=1.0`** (`executor/paper.py:12`). Paper
  v2 should model queue position. Affects the leg-fail probability
  parameter calibration.
