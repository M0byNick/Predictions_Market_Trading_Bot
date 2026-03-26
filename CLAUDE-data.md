# Database, Calibration & Data Sources

## SQLite Database (`data/kalshi.db`)

WAL mode enabled for concurrent reads (dashboard, calibration) while bot writes. Initialized by `db.py:init_db()` — all tables use `CREATE TABLE IF NOT EXISTS`.

### Tables

**`trades`** — Append-only trade audit trail
| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| ticker | TEXT | Kalshi market ticker (e.g., KXHIGHCHI-26MAR23-B46.5) |
| category | TEXT | crypto, weather, or economics |
| side | TEXT | yes or no |
| your_prob | REAL | Model's estimated probability |
| market_prob | REAL | Market price as probability at entry |
| edge_at_entry | REAL | your_prob - market_prob (adjusted for side) |
| num_contracts | INTEGER | Number of contracts purchased |
| cost_usd | REAL | Total cost in USD |
| kelly_fraction | REAL | Capped Kelly fraction used (after MAX_BET cap) |
| full_kelly | REAL | True Kelly f* = edge / (1 - market_prob) |
| fractional_kelly | REAL | full_kelly × KELLY_FRACTION (quarter-Kelly) |
| kelly_rec_usd | REAL | Dollar amount Kelly recommends at current bankroll |
| kelly_multiplier | REAL | actual_cost / kelly_rec_usd (live deployment scaling factor) |
| entry_time | TEXT | ISO 8601 UTC |
| outcome | TEXT | win, loss, or NULL (open) — auto-set by `check_settlements()` |
| settlement_price | REAL | 1.0 or 0.0 — auto-set by `check_settlements()` |
| pnl_usd | REAL | Realized P&L — auto-set by `check_settlements()` |
| settlement_time | TEXT | ISO 8601 UTC — auto-set by `check_settlements()` |
| notes | TEXT | Screener rationale |

**`pending_orders`** — Crash recovery for in-flight orders
- ticker, side, contracts, cost_usd, order_id, timestamp

**`market_snapshots`** — Decision audit trail (every market evaluated)
- timestamp, cycle_id, ticker, category, decision, data (JSON blob)

**`daily_pnl`** — Aggregated daily metrics per category
- date, category, realized_pnl, trade_count, win_count, loss_count
- UNIQUE(date, category) with ON CONFLICT upsert

### Schema Migrations
- `_migrate_kelly_columns()`: Adds full_kelly, fractional_kelly, kelly_rec_usd, kelly_multiplier via ALTER TABLE if missing
- Legacy JSON migration: `migrate_json_trades()`, `migrate_json_pending()`, `migrate_jsonl_snapshots()` — run once on first init

## Calibration System (`calibration.py`)

The most important feedback loop. Answers: "Are my probability estimates actually calibrated?"

### Metrics
- **Brier score**: Mean squared error of predicted vs actual. 0.0 = perfect, 0.25 = random.
- **Calibration table**: Group trades by predicted probability decile, compare to actual win rate per bucket. Perfect = diagonal.
- **Confidence bias**: Average (predicted - actual). Positive = overconfident, negative = underconfident. Threshold: ±0.02.
- **Edge decay**: Monthly rolling average edge and P&L per trade. Detects if models are going stale.
- **Rolling Brier**: Weekly Brier score windows to detect calibration drift over time.

### CLI
```bash
python calibration.py                    # Full report
python calibration.py --category crypto  # Single category
python calibration.py --export           # Export to data/calibration.csv
```

### Integration
- `main.py --calibration` — prints report
- `alerts.py:send_calibration_digest()` — weekly Telegram summary
- `alerts.py:send_daily_digest()` — includes calibration drift warnings
- `dashboard.py` — reliability diagram (predicted vs actual) panel

## External Data Sources

| Source | Used By | Auth | Rate Limit |
|---|---|---|---|
| Kalshi REST API | kalshi_client.py | RSA-PSS signature (API key ID + private key) | 20 req/sec |
| CoinGecko | screeners/crypto.py | None (free tier) | ~10 req/min |
| NWS API | screeners/weather.py | None (free, no key) | Reasonable use |
| FRED API | screeners/economics.py | Free API key (env var) | 120 req/min |

### Kalshi API Notes
- Base URL: `https://api.elections.kalshi.com/trade-api/v2`
- Auth header: `KALSHI-ACCESS-KEY` = API key ID (not email)
- Signature: RSA-PSS with SHA-256, message = `{timestamp_ms}{METHOD}{path}`
- Market prices returned as dollar strings (e.g., `last_price_dollars: "0.6500"`)
- Use `screeners.utils.get_market_prob()` to handle field format differences

### Environment Variables (`.env`)
```
KALSHI_EMAIL=...
KALSHI_API_ID=...
KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
FRED_API_KEY=...
```
