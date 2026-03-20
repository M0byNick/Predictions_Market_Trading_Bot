# Kalshi Prediction Market Bot

A Python-based system for screening, sizing, tracking, and executing prediction market trades on Kalshi across three categories: **crypto events**, **weather/climate**, and **economic data releases**.

## Architecture

```
main.py                  ← Orchestrator: runs screeners, alerts, and optional auto-execution
├── config.py            ← API keys, Telegram token, thresholds, allocation splits
├── kalshi_client.py     ← Kalshi REST API wrapper (auth, orders, positions, market data)
├── kelly.py             ← Kelly criterion sizing (fractional Kelly with bankroll management)
├── tracker.py           ← Trade logging, P&L tracking, edge measurement over time
├── alerts.py            ← Telegram bot for mobile notifications + trade approval
└── screeners/
    ├── crypto.py        ← BTC/ETH/SOL price bracket screener with sentiment signals
    ├── weather.py       ← NWS/GFS-based temperature and precipitation screener
    └── economics.py     ← CPI, Fed, jobs data release screener
```

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure `config.py`:**
   - Add your Kalshi API credentials (RSA key pair — generate via Kalshi dashboard)
   - Add your Telegram bot token (create via @BotFather) and chat ID
   - Set your bankroll and allocation splits

3. **Generate Kalshi API keys:**
   - Log into Kalshi → Settings → API Keys
   - Generate an RSA key pair
   - Save the private key as `kalshi_private_key.pem` in the project root

4. **Set up Telegram alerts:**
   - Message @BotFather on Telegram → `/newbot` → follow prompts
   - Copy the bot token into `config.py`
   - Message your new bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat_id

5. **Run the bot:**
   ```bash
   python main.py              # Full screener + alert loop
   python main.py --backtest   # Review historical edge from tracker data
   ```

## Strategy Overview

The bot screens for mispriced contracts using category-specific models, sizes positions via fractional Kelly criterion, and alerts you on Telegram with one-tap approve/reject before executing. Every trade is logged for ongoing edge measurement.

**Allocation (default, adjustable):** 50% crypto events, 30% weather/climate, 20% economics.

## Tracking

All trades are logged to `data/trades.json` with fields: market, your estimated probability, market price at entry, Kelly-optimal size, actual size, outcome, and P&L. The `tracker.py` module computes rolling edge, hit rate, and bankroll growth curves so you can tell signal from noise after ~50+ trades.

## Regulatory Notes

This system is designed for use from New York and New Jersey. Kalshi is CFTC-regulated and accessible in both states. Sports betting contracts are excluded (blocked in NY). All crypto spot positions should be held on BitLicense-compliant exchanges (Coinbase, Gemini).
