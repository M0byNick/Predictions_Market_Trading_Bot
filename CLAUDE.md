# Predictions Market Trading Bot

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
python -m pytest tests/      # Run unit tests (100 tests)
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
alerts.py            # Telegram notifications + inline buttons + daily digest
backtest.py          # Screener validation against settled markets
calibration.py       # Calibration analysis (Brier, edge decay, bias detection)
dashboard.py         # Streamlit dashboard (bankroll curve, calibration, P&L)
snapshots.py         # Market snapshot logger (SQLite backend)
screeners/
  __init__.py        # Screener imports
  utils.py           # get_market_prob() — handles Kalshi API field formats
  crypto.py          # Log-normal vol model vs Kalshi crypto brackets
  weather.py         # NWS forecast vs Kalshi temp contracts
  economics.py       # FRED-based heuristic flags on econ releases
tests/
  test_kelly.py            # Kelly sizing edge cases (21 tests)
  test_screener_parsing.py # Strike/threshold/date parsing (28 tests)
  test_tracker.py          # Trade logging, metrics, pending orders (22 tests)
  test_paper_client.py     # Paper order/settlement simulation (13 tests)
  test_calibration.py      # Calibration analysis (16 tests)
data/
  kalshi.db          # SQLite database (trades, pending orders, snapshots, daily P&L)
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

## Current State (2026-03-23)
- **Tier 1**: Secrets, logging, paper trading, backtesting
- **Tier 2**: Crash recovery, market snapshots, unit tests, health alerts
- **Tier 3**: SQLite migration, calibration analysis, Streamlit dashboard, mobile workflow, Kelly sizing columns
- **Paper trading**: $1M bankroll, auto-execute all strategies, ~$500 max per position
- **100 unit tests passing**
- **307+ paper trades logged** across weather, crypto, economics
- **Pending**: Screener enhancements (GFS weather, quantitative econ model, on-chain crypto signals)
- **Remote**: git@github.com:M0byNick/Predictions_Market_Trading_Bot.git (private)
- **Directory**: ~/Documents/2026/Professional/Trading/Prediction_Markets/
