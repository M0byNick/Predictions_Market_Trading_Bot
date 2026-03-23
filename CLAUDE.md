# Predictions Market Trading Bot

## Quick Start
```bash
python main.py --once    # Screen markets, print results (no trades)
python main.py           # Full loop (screen -> alert -> trade -> sleep)
python main.py --dry-run # Run one full cycle and exit (CI-friendly)
python main.py --calibration  # Print calibration report
python main.py --summary # Print performance summary
python backtest.py       # Validate screeners against settled markets
python calibration.py    # Detailed calibration analysis (--export for CSV)
streamlit run dashboard.py  # Launch Streamlit dashboard
python -m pytest tests/  # Run unit tests
```

## Project Structure
```
main.py              # Entry point, orchestrator (with crash recovery)
config.py            # All params, loads secrets from .env
log.py               # Centralized logging (console + data/bot.log)
db.py                # SQLite database layer (WAL mode, migrations)
kalshi_client.py     # Kalshi REST API with RSA auth
paper_client.py      # Simulated order fills (extends KalshiClient)
kelly.py             # Kelly criterion position sizing
tracker.py           # Trade journal, P&L, Brier score (SQLite backend)
alerts.py            # Telegram notifications + inline buttons + daily digest
backtest.py          # Screener validation against settled markets
calibration.py       # Calibration analysis (Brier, edge decay, bias detection)
dashboard.py         # Streamlit dashboard (bankroll curve, calibration, P&L)
snapshots.py         # Market snapshot logger (SQLite backend)
screeners/
  crypto.py          # Log-normal vol model vs Kalshi crypto brackets
  weather.py         # NWS forecast vs Kalshi temp contracts
  economics.py       # Heuristic flags on econ releases (alert-only)
tests/
  test_kelly.py      # Kelly sizing edge cases
  test_screener_parsing.py  # Strike/threshold/date parsing
  test_tracker.py    # Trade logging, metrics, pending orders, SQLite
  test_paper_client.py      # Paper order/settlement simulation
  test_calibration.py       # Calibration analysis tests
```

## Key Conventions
- Python 3.9 — use `from __future__ import annotations` (not `X | None` at runtime)
- Logging via `from log import logger` — never use `print()` for operational output
- Secrets via env vars / `.env` file — never hardcode credentials
- `PAPER_TRADING = True` by default — must explicitly disable for live trading
- Economics screener is alert-only (`ECON_ALERT_ONLY = True`)
- All data stored in SQLite (`data/kalshi.db`) with WAL mode
- Legacy JSON files auto-migrated to SQLite on first run
- Market snapshots logged to SQLite `market_snapshots` table
- Inline keyboard buttons for mobile-friendly trade approval

## Current State (2026-03-22)
- Tier 1 complete: secrets, logging, paper trading, backtesting
- Tier 2 complete: crash recovery, market snapshots, unit tests (82), health alerts
- Tier 3 complete: SQLite migration, calibration analysis, dashboard, mobile workflow
- 100 unit tests passing
- Remote: git@github.com:M0byNick/Predictions_Market_Trading_Bot.git (private)
