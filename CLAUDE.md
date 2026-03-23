# Predictions Market Trading Bot

## Quick Start
```bash
python main.py --once    # Screen markets, print results (no trades)
python main.py           # Full loop (screen → alert → trade → sleep)
python backtest.py       # Validate screeners against settled markets
python -m pytest tests/  # Run unit tests
```

## Project Structure
```
main.py              # Entry point, orchestrator (with crash recovery)
config.py            # All params, loads secrets from .env
log.py               # Centralized logging (console + data/bot.log)
kalshi_client.py     # Kalshi REST API with RSA auth
paper_client.py      # Simulated order fills (extends KalshiClient)
kelly.py             # Kelly criterion position sizing
tracker.py           # Trade journal, P&L, Brier score, pending orders
alerts.py            # Telegram notifications + approval + health checks
backtest.py          # Screener validation against settled markets
snapshots.py         # JSONL market snapshot logger (decision audit trail)
screeners/
  crypto.py          # Log-normal vol model vs Kalshi crypto brackets
  weather.py         # NWS forecast vs Kalshi temp contracts
  economics.py       # Heuristic flags on econ releases (alert-only)
tests/
  test_kelly.py      # Kelly sizing edge cases
  test_screener_parsing.py  # Strike/threshold/date parsing
  test_tracker.py    # Trade logging, metrics, pending orders
  test_paper_client.py      # Paper order/settlement simulation
```

## Key Conventions
- Python 3.9 — use `from __future__ import annotations` (not `X | None` at runtime)
- Logging via `from log import logger` — never use `print()` for operational output
- Secrets via env vars / `.env` file — never hardcode credentials
- `PAPER_TRADING = True` by default — must explicitly disable for live trading
- Economics screener is alert-only (`ECON_ALERT_ONLY = True`)
- All JSON writes are atomic (write tmp → os.replace) to prevent corruption
- Market snapshots logged to `data/snapshots.jsonl` (JSONL, append-only)

## Current State (2026-03-22)
- Tier 1 complete: secrets, logging, paper trading, backtesting
- Tier 2 complete: crash recovery, market snapshots, unit tests (82), health alerts
- Next: Tier 3 — calibration analysis, SQLite migration, dashboards
- Remote: git@github.com:M0byNick/Predictions_Market_Trading_Bot.git (private)
