# Trading Pipeline & Risk Management

## Execution Flow

```
Screeners (30m interval)
    → Opportunities (ticker, side, your_prob, market_prob, edge)
    → Kelly sizing (full_kelly → fractional → capped)
    → Signal-only check (SIGNAL_ONLY config)
    → Econ alert-only check (ECON_ALERT_ONLY config)
    → Approval check (REQUIRE_APPROVAL config — inline TG buttons)
    → Pending order marked (crash recovery)
    → Order placed (paper or live client)
    → Fill checked (partial fills handled)
    → Trade logged to SQLite with Kelly columns
    → Pending cleared
    → Telegram confirmation sent
```

## Kelly Criterion Sizing (`kelly.py`)

**Full Kelly**: `f* = edge / (1 - market_prob)` for binary bets
- `edge = your_prob - market_prob` (YES side)
- For NO side: probabilities are flipped before calculation

**Fractional Kelly**: `f* × KELLY_FRACTION` (default 0.25 = quarter-Kelly)
- Quarter-Kelly gives ~75% of growth rate with far less variance

**Safety rails**:
- `MIN_EDGE_THRESHOLD = 0.05` — no trade below 5% edge
- `MAX_BET_FRACTION = 0.0015` — caps position at ~$500 on $1M bankroll
- Minimum position $5 — skip if sizing is trivially small
- Economics uses reduced Kelly (0.10) due to heuristic edge

**Kelly columns stored per trade** (added Tier 3):
| Column | Description |
|---|---|
| `full_kelly` | True Kelly fraction (e.g., 0.62 = 62% of bankroll) |
| `fractional_kelly` | full_kelly × 0.25 (quarter-Kelly) |
| `kelly_rec_usd` | Dollar size at current bankroll (fractional_kelly × category_bankroll) |
| `kelly_multiplier` | actual_cost / kelly_rec_usd (scale-down factor for live deployment) |

**Bankroll allocation** (`config.py`):
- Crypto: 33.3% ($333K of $1M paper)
- Weather: 33.3%
- Economics: 33.4%

## Paper Trading (`paper_client.py`)

`PaperClient` extends `KalshiClient`:
- **Market data**: Real Kalshi API (live prices, orderbooks)
- **Orders**: Simulated locally with instant fills at requested price
- **Balance/positions**: Tracked in `data/paper_trades.json`
- **Settlement**: Manual via `settle(ticker, result)` method

Current config: $1M bankroll, auto-execute all strategies, ~$500 max position.

## Crash Recovery

1. Before order placement: `tracker.mark_pending(ticker, side, contracts, cost)`
2. After fill confirmation: `tracker.clear_pending(ticker, side)`
3. On restart: `reconcile_pending_orders()` checks each pending order's fill status via API
4. Orphaned orders are either logged as recovered trades or cleared with alerts

## Telegram Alerts (`alerts.py`)

**Trade alerts**: Inline keyboard buttons (APPROVE/REJECT) for mobile one-tap workflow
**Execution confirmations**: Ticker, contracts, cost
**Settlement notifications**: Outcome, P&L with green/red emoji
**Health checks**: Every 6 cycles (3h) — uptime, balance, last opportunity
**No-opportunity alerts**: After 24h with no screener hits
**Daily digest**: Open positions, P&L, calibration drift warnings
**Expiration alerts**: 24h and 1h before contract settlement

## Current Config State
```
PAPER_TRADING = True
REQUIRE_APPROVAL = False      # Auto-execute for data gathering
ECON_ALERT_ONLY = False       # Execute econ trades too
SIGNAL_ONLY = False
TOTAL_BANKROLL = 1,000,000
MAX_BET_FRACTION = 0.0015     # ~$500 cap per position
KELLY_FRACTION = 0.25         # Quarter-Kelly
MIN_EDGE_THRESHOLD = 0.05     # 5% minimum edge
SCREENER_INTERVAL_MINUTES = 30
```
