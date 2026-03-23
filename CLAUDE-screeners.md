# Screeners Architecture

Three screeners run every 30 minutes, each targeting a different Kalshi market category.

## Crypto Screener (`screeners/crypto.py`)

**Model**: Log-normal diffusion (Black-Scholes-style)
- P(S_T > K) = N(d2) where d2 = (ln(S/K) - 0.5σ²T) / (σ√T)
- Assumes zero drift (conservative)
- 30-day realized volatility from CoinGecko, cached 4 hours in `data/vol_cache.json`

**Markets monitored**:
- Series: `KXBTC` (Bitcoin), `KXETH` (Ethereum), `KXSOL` (Solana)
- Timeframes: Weekly and monthly price brackets (15-min contracts filtered out)
- Settlement: CF Benchmarks Real-Time Index, 60-second average at expiry

**Data sources**:
- CoinGecko free API — spot prices (no key needed)
- CoinGecko market_chart — 30-day daily prices for realized vol

**Known limitations**:
- Vol model is simple — doesn't account for vol regimes, skew, or term structure
- No on-chain signals yet (planned: fear/greed, exchange flows, funding rates)
- Contracts expiring < 2 hours are filtered (was 1 day, relaxed in Tier 3)

## Weather Screener (`screeners/weather.py`)

**Model**: Normal distribution around NWS point forecast
- P(high > threshold) = 1 - Φ((threshold - forecast) / σ)
- σ = forecast uncertainty, calibrated by days-out (closer = tighter)
- Default σ ≈ 2.5°F for 1-day forecasts

**Markets monitored**:
- `KXHIGHNY` — NYC daily high temp
- `KXHIGHCHI` — Chicago daily high temp
- `KXHIGHMIA` — Miami daily high temp
- `KXHIGHAUS` — Austin daily high temp

**Data sources**:
- NWS API (`api.weather.gov`) — free, no key, point forecasts + hourly
- Settlement source: NWS Daily Climate Report / NOWData (same source = built-in edge)

**Known limitations**:
- Single-model forecast (NWS only) — no ensemble with GFS/ECMWF yet
- Normal distribution assumption may not capture tail weather events
- σ calibration is static — should be dynamic based on forecast skill by lead time

## Economics Screener (`screeners/economics.py`)

**Model**: Heuristic-based (not quantitative)
- Flags: trend disagreement, cheap tails within recent range, threshold proximity
- Uses FRED API for historical data trends
- Reduced Kelly fraction (0.10 vs 0.25) due to lower confidence

**Markets monitored**:
- `KXCPI` — Monthly CPI change (e.g., "Will CPI rise >0.8% in March?")
- `KXCPIYOY` — CPI year-over-year
- `KXFED` — Federal funds rate after FOMC meetings

**Data sources**:
- FRED API (free key from fred.stlouisfed.org) — historical CPI, fed funds, unemployment
- Series IDs: CPIAUCSL, CPIAUCNS, DFEDTARU, UNRATE, PAYEMS, GDP

**Known limitations**:
- Edge estimates are heuristic, not model-driven — manual review recommended
- No consensus estimate tracking yet (planned: Bloomberg/FRED survey data)
- No BLS release schedule automation

## Shared Utilities (`screeners/utils.py`)

**`get_market_prob(market)`**: Extracts probability (0-1) from Kalshi market dict. Handles both:
- Current API: `last_price_dollars`, `yes_ask_dollars` (string dollar values like "0.6500")
- Legacy API: `last_price`, `yes_ask` (integer cents)

All screeners must use this function — never read price fields directly.

## Snapshot Logging

Every market evaluated (trade or skip) is logged to SQLite `market_snapshots` table via `snapshots.py`. Fields include: ticker, category, decision, model probability, market probability, edge. Grouped by `cycle_id` for per-screening-cycle analysis.
