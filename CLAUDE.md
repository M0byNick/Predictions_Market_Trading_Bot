# Predictions Market Trading Bot

## Quick Start
```bash
python main.py --once    # Screen markets, print results (no trades)
python main.py           # Full loop (screen → alert → trade → sleep)
python backtest.py       # Validate screeners against settled markets
```

## Project Structure
```
main.py              # Entry point, orchestrator
config.py            # All params, loads secrets from .env
log.py               # Centralized logging (console + data/bot.log)
kalshi_client.py     # Kalshi REST API with RSA auth
paper_client.py      # Simulated order fills (extends KalshiClient)
kelly.py             # Kelly criterion position sizing
tracker.py           # Trade journal, P&L, Brier score
alerts.py            # Telegram notifications + approval flow
backtest.py          # Screener validation against settled markets
screeners/
  crypto.py          # Log-normal vol model vs Kalshi crypto brackets
  weather.py         # NWS forecast vs Kalshi temp contracts
  economics.py       # Heuristic flags on econ releases (alert-only)
```

## Key Conventions
- Python 3.9 — use `from __future__ import annotations` (not `X | None` at runtime)
- Logging via `from log import logger` — never use `print()` for operational output
- Secrets via env vars / `.env` file — never hardcode credentials
- `PAPER_TRADING = True` by default — must explicitly disable for live trading
- Economics screener is alert-only (`ECON_ALERT_ONLY = True`)

## Current State (2026-03-22)
- Tier 1 infrastructure complete (secrets, logging, paper trading, backtesting)
- Next: Tier 2 — crash recovery, market snapshots, unit tests, health alerts
- Remote: git@github.com:M0byNick/Predictions_Market_Trading_Bot.git (private)
