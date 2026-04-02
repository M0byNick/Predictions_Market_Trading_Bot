# Screeners Architecture

Three screeners run every 30 minutes, each targeting a different Kalshi market category.

## Crypto Screener (`screeners/crypto.py`)

**Model**: Log-normal diffusion with sentiment drift (Phase 5)
- P(S_T > K) = N(d2) where d2 = (ln(S/K) + (μ - 0.5σ²)T) / (σ√T)
- μ = sentiment-derived drift from Fear & Greed Index (contrarian: fear→bullish, greed→bearish)
- Drift capped at ±5% annualized (`SENTIMENT_MAX_DRIFT`), toggled via `USE_SENTIMENT_SIGNALS`
- 30-day realized volatility from CoinGecko, cached 4 hours in `data/vol_cache.json`
- FGI cached alongside vol with same 4h TTL

**Markets monitored**:
- Series: `KXBTC` (Bitcoin), `KXETH` (Ethereum), `KXSOL` (Solana)
- Timeframes: Weekly and monthly price brackets (15-min contracts filtered out)
- Settlement: CF Benchmarks Real-Time Index, 60-second average at expiry

**Data sources**:
- CoinGecko free API — spot prices (no key needed)
- CoinGecko market_chart — 30-day daily prices for realized vol
- Alternative.me Fear & Greed Index API — free, no key (Phase 5)

**Known limitations**:
- Vol model doesn't account for vol regimes, skew, or term structure
- FGI is contrarian only — no exchange flow or funding rate signals yet
- Contracts expiring < 2 hours are filtered (was 1 day, relaxed in Tier 3)

## Weather Screener (`screeners/weather.py`)

**Model**: Normal distribution around NWS point forecast with dynamic sigma (Phase 5)
- P(high > threshold) = 1 - Φ((threshold - forecast) / σ)
- σ = dynamic from NWS gridpoint temperature spread (min/max → 90% CI → σ = spread/3.29)
- Falls back to static σ lookup by days-out if gridpoint data unavailable
- Static defaults: 2.0°F (same-day) to 5.5°F (7-day), floor 1.5°F for dynamic
- Toggled via `USE_DYNAMIC_SIGMA` config

**Markets monitored**:
- `KXHIGHNY` — NYC daily high temp
- `KXHIGHCHI` — Chicago daily high temp
- `KXHIGHMIA` — Miami daily high temp
- `KXHIGHAUS` — Austin daily high temp

**Data sources**:
- NWS API (`api.weather.gov`) — free, no key, point forecasts + hourly
- NWS gridpoints API — temperature min/max arrays for dynamic sigma (Phase 5)
- Settlement source: NWS Daily Climate Report / NOWData (same source = built-in edge)

**Known limitations**:
- Normal distribution assumption may not capture tail weather events
- Dynamic sigma uses NWS gridpoint spread, not raw GFS ensemble members
- No ECMWF integration

## Economics Screener (`screeners/economics.py`)

**Model**: Quantitative normal distribution (Phase 5, replaces heuristics)
- CPI/NONFARM: fits N(μ, σ²) to month-over-month changes, P(next_change > threshold)
- CPI_YOY/GDP/UNEMPLOYMENT: fits N(μ, σ²) to level distribution
- FED_RATE: level-based with σ=0.50×√(months_to_meeting), reflects rate cycle uncertainty
- Heuristic flags (trend, cheap tail, near threshold) kept as rationale evidence only
- Kelly fraction 0.15 (bumped from 0.10 — model-based edge warrants more confidence)

**Markets monitored**:
- `KXCPI` — Monthly CPI change (e.g., "Will CPI rise >0.8% in March?")
- `KXCPIYOY` — CPI year-over-year
- `KXFED` — Federal funds rate after FOMC meetings

**Data sources**:
- FRED API (free key from fred.stlouisfed.org) — historical CPI, fed funds, unemployment
- Series IDs: CPIAUCSL, CPIAUCNS, DFEDTARU, UNRATE, PAYEMS, GDP

**Known limitations**:
- Normal distribution may not capture fat tails in economic data
- No consensus estimate tracking yet (planned: Bloomberg/FRED survey data)
- No BLS release schedule automation
- Fed rate σ scales with √(months) but no dot plot or futures curve integration
- CPI forward months: σ grows by √(months_ahead) but no structural break detection

## Shared Utilities (`screeners/utils.py`)

**`get_market_prob(market)`**: Extracts probability (0-1) from Kalshi market dict. Handles both:
- Current API: `last_price_dollars`, `yes_ask_dollars` (string dollar values like "0.6500")
- Legacy API: `last_price`, `yes_ask` (integer cents)

All screeners must use this function — never read price fields directly.

## Polymarket Edge Validation (`polymarket_client.py`)

Read-only cross-market price comparison for crypto screener. Fetches prices from Polymarket's CLOB API to compare against our model probabilities. No trading, no auth.

- **API**: CLOB at `https://clob.polymarket.com/markets` (free, public, paginated by creation date)
- **Matching**: Searches for "Bitcoin above $X" / "between $X-$Y" markets by asset keyword + strike + expiry date
- **Cache**: In-memory, 10-minute TTL
- **Coverage**: Crypto only (BTC, ETH, SOL). Weather/econ markets don't exist on Polymarket in matching format.
- **Output**: `polymarket_prob` field in snapshot dict. Not used for trade decisions yet — data collection for calibration analysis.

**Known limitations**:
- CLOB API requires paginating through 750K+ markets to reach current daily contracts
- Polymarket crypto markets are mostly intraday (5-min Up/Down) — "above $X at 5PM ET" markets are better matches but less common
- No Fed rate, CPI, or weather markets on Polymarket in Kalshi-compatible format

## Snapshot Logging

Every market evaluated (trade or skip) is logged to SQLite `market_snapshots` table via `snapshots.py`. Fields include: ticker, category, decision, model probability, market probability, Polymarket probability, edge. Grouped by `cycle_id` for per-screening-cycle analysis.
