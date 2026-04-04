# Predictions Market Trading Bot

## Security
- **NO AXIOS**: Do NOT use Axios or `npm install axios` in any JavaScript code. Major external security vulnerability. Use native `fetch` API instead.

Automated screener and paper-trading system for Kalshi prediction markets. Targets three categories: crypto price brackets, weather (daily high temps), and economic data releases (CPI, Fed rate). NY/NJ compliant (CFTC-regulated, non-sports markets only).

## Quick Start
```bash
python main.py --once        # Screen markets once, print results (no trades)
python main.py               # Full loop (screen -> alert -> trade -> sleep 30m)
python main.py --dry-run     # One full cycle then exit (CI-friendly)
python main.py --calibration # Print calibration report
python main.py --summary     # Print performance summary + export CSV
python backtest.py           # Validate screeners against settled markets
python calibration.py        # Detailed calibration analysis (--export for CSV)
streamlit run dashboard.py   # Launch Streamlit dashboard
python -m pytest tests/      # Run unit tests (168 tests)
```

## Project Structure
```
main.py              # Entry point, orchestrator, crash recovery, CLI flags
config.py            # All params, loads secrets from .env
log.py               # Centralized logging (console + data/bot.log)
db.py                # SQLite database layer (WAL mode, schema migrations)
kalshi_client.py     # Kalshi REST API with RSA-PSS auth
paper_client.py      # Simulated order fills (extends KalshiClient)
kelly.py             # Kelly criterion position sizing
tracker.py           # Trade journal, P&L, Brier score (SQLite backend)
alerts.py            # Telegram notifications + inline buttons + daily digest + command handler
polymarket_client.py # Read-only Polymarket Gamma/CLOB API for edge validation
backtest.py          # Screener validation against settled markets
calibration.py       # Calibration analysis (Brier, edge decay, bias detection)
dashboard.py         # Streamlit dashboard (bankroll curve, calibration, P&L)
snapshots.py         # Market snapshot logger (SQLite backend)
screeners/
  __init__.py        # Screener imports
  utils.py           # get_market_prob() — handles Kalshi API field formats
  crypto.py          # Log-normal vol model + FGI sentiment drift, bracket/threshold handling
  weather.py         # NWS forecast + dynamic sigma, tail dampening, bracket/less/greater handling
  economics.py       # FRED-based quantitative model on econ releases (Phase 5)
tests/
  test_kelly.py            # Kelly sizing edge cases (21 tests)
  test_screener_parsing.py # Strike/threshold/date parsing + Phase 5 model tests (52 tests)
  test_tracker.py          # Trade logging, metrics, pending orders, auto-settlement, guardrails (37 tests)
  test_paper_client.py     # Paper order/settlement simulation (14 tests)
  test_calibration.py      # Calibration analysis + diagnostics (28 tests)
data/
  kalshi.db          # SQLite database (trades, pending orders, snapshots, daily P&L,
                     #   price_checks, skipped_opportunities, weather_actuals)
  bot.log            # Rotating log file
```

See also:
- `CLAUDE-screeners.md` — screener architecture, models, series tickers
- `CLAUDE-trading.md` — execution pipeline, Kelly sizing, risk management
- `CLAUDE-data.md` — database schema, calibration, data sources

## Key Conventions
- Python 3.9 — use `from __future__ import annotations` (not `X | None` at runtime)
- Logging via `from log import logger` — never use `print()` for operational output
- Secrets via env vars / `.env` file — never hardcode credentials
- `PAPER_TRADING = True` by default — must explicitly disable for live trading
- All data stored in SQLite (`data/kalshi.db`) with WAL mode
- Legacy JSON files auto-migrated to SQLite on first run
- Atomic writes for crash safety throughout
- All market price reads go through `screeners.utils.get_market_prob()` for API compat

## Current State (2026-04-04)
- **Tier 1**: Secrets, logging, paper trading, backtesting
- **Tier 2**: Crash recovery, market snapshots, unit tests, health alerts
- **Tier 3**: SQLite migration, calibration analysis, Streamlit dashboard, mobile workflow, Kelly sizing columns
- **Phase 5**: Screener enhancements — all three complete:
  - Crypto: Fear & Greed Index sentiment drift overlay (contrarian, ±5% max drift)
  - Weather: Dynamic sigma from NWS gridpoint temperature spread (static fallback)
  - Economics: Quantitative normal distribution model replacing heuristic flags
- **Contract-type handling**: Bracket (`between`), less-than (`less`), greater-than (`greater`) correctly handled across all screeners — was the root cause of 87% of pre-fix losses
- **Risk guardrails**:
  - Max edge threshold (50%) — blocks implausible model claims
  - Per-ticker position limit ($500) — prevents stacking across cycles
  - Weather tail dampening — power dampening on far-from-forecast probabilities
  - Weather market-informed blending — blends model toward market at ≤5¢
  - Economics forward-month uncertainty — σ scales by √(months) for CPI/Fed rate
  - Paper balance enforcement — rejects trades when balance < cost
  - Penny market floor (≤5¢) — 0/29 win rate, cut until tail models improve
  - Kelly post-rounding verification — actual_cost checked within budget after int() truncation
- **Enhanced calibration**: Expected vs realized edge, Brier by days-out, penny market split, phase comparison, Polymarket cross-validation
- **Polymarket validation**: Read-only price comparison via Gamma/CLOB API for crypto markets — logs `polymarket_prob` in snapshots for post-hoc calibration analysis
- **Analytics tracking** (added 2026-04-03):
  - Post-entry price tracking — re-prices open positions each cycle, stores in `price_checks` table
  - Missed-win tracking — logs guardrail-skipped trades in `skipped_opportunities`, backfills settlement results
  - NWS actual temperature logging — stores forecast vs observed temps in `weather_actuals` on settlement
  - Entry timing analysis — captures `entry_hour`, `entry_vol`, `entry_fgi` per trade for time-of-day patterns
  - Automated decision triggers — calibration report flags when data supports strategy changes
- **Auto-settlement**: Every cycle checks open trades against Kalshi API (`status == "finalized"`), settles wins/losses, updates P&L + paper balance
- **Paper trading**: $1M bankroll (reset 2026-03-26), balance $730,278, auto-execute all strategies, ~$500 max per position
- **Dashboard**: Streamlit at localhost:8501, auto-refresh 30s, Phase 5+ only view (excludes pre-fix buggy trades)
- **Bot running**: with watchdog auto-restart (`watchdog.sh`)
- **Telegram**: Bot token configured, chat ID = 862997381, command handler responds to /status /balance /dashboard /help
- **168 unit tests passing**
- **198 Phase 5+ trades** (95 settled, 103 open, 12 wins, P&L -$3,000), 665 total trades in DB
- **Hourly CLAUDE.md update**: Scheduled task runs every hour to keep docs current
- **Pending**: Accumulate post-fix trade data for calibration evaluation, live trading readiness
- **Remote**: git@github.com:M0byNick/Predictions_Market_Trading_Bot.git (private)
- **Directory**: ~/Documents/2026/Professional/Trading/Prediction_Markets/
