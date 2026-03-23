"""
Configuration for the Kalshi prediction market bot.

Secrets are loaded from environment variables (or a .env file).
Strategy parameters are set below and can be tuned without touching secrets.
"""
import os
from pathlib import Path

# Load .env file if present (no dependency required)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))

# ── Kalshi API ───────────────────────────────────────────────────────────────
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_EMAIL = os.environ.get("KALSHI_EMAIL", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")

# ── Telegram Alerts ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Bankroll & Allocation ────────────────────────────────────────────────────
TOTAL_BANKROLL = 5000       # Starting bankroll in USD
ALLOCATION = {
    "crypto":    0.50,      # 50% to crypto event contracts
    "weather":   0.30,      # 30% to weather/climate contracts
    "economics": 0.20,      # 20% to economic data releases
}

# ── Kelly Criterion Parameters ───────────────────────────────────────────────
# Fractional Kelly multiplier — 0.25 is quarter-Kelly (conservative).
# Full Kelly (1.0) maximizes long-run growth but has brutal drawdowns.
# Quarter Kelly gives ~75% of the growth rate with far less variance.
KELLY_FRACTION = 0.25

# Maximum percentage of category bankroll on a single trade.
# Even if Kelly says bet 40%, this caps it. Safety rail.
MAX_BET_FRACTION = 0.15

# Minimum edge (your_prob - market_prob) to consider a trade.
# Below this threshold, transaction costs and model uncertainty eat the edge.
MIN_EDGE_THRESHOLD = 0.05   # 5 percentage points

# ── Screener Settings ────────────────────────────────────────────────────────
SCREENER_INTERVAL_MINUTES = 30   # How often to re-scan markets

# Crypto screener: which tickers and timeframes to monitor
CRYPTO_TICKERS = ["BTC", "ETH", "SOL"]
CRYPTO_TIMEFRAMES = ["weekly", "monthly"]  # Skip 15-min (day trading)

# Weather screener: target cities for daily high temp contracts
WEATHER_CITIES = ["NYC", "CHI", "MIA", "AUS"]
# NWS API endpoint for forecast data (free, no key needed)
NWS_API_BASE = "https://api.weather.gov"

# Economics screener: events to track
ECON_EVENTS = [
    "CPI",          # Monthly CPI release
    "FED_RATE",     # FOMC rate decisions
    "NONFARM",      # Nonfarm payrolls
    "GDP",          # GDP estimates
    "UNEMPLOYMENT", # Unemployment rate
]
# FRED API key (free from https://fred.stlouisfed.org/docs/api/api_key.html)
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# ── Economics Screener Overrides ─────────────────────────────────────────
# Economics edge estimates are heuristic, not model-driven.
# Default to alert-only: send Telegram notifications but do not execute.
ECON_ALERT_ONLY = True
# If overridden to auto-trade, use a much smaller Kelly fraction.
ECON_MAX_KELLY_FRACTION = 0.10  # vs 0.25 for crypto/weather

# ── Paper Trading ────────────────────────────────────────────────────────────
# If True, orders are simulated locally — real market data, fake fills.
# Tracks balance and positions in data/paper_trades.json.
PAPER_TRADING = True  # Set to False for live trading

# ── Trade Execution ──────────────────────────────────────────────────────────
# If True, the bot will send Telegram alerts and wait for your approval
# before placing orders. If False, it auto-executes (use with caution).
REQUIRE_APPROVAL = True

# Order type: "limit" places at your target price, "market" fills immediately.
# Limit orders are strongly recommended — they define your entry price.
DEFAULT_ORDER_TYPE = "limit"

# ── Health Checks ───────────────────────────────────────────────────────────
# Send a heartbeat every N screening cycles (e.g., 6 × 30min = 3h)
HEALTH_CHECK_INTERVAL_CYCLES = 6
# Alert if no opportunities found for this many hours
NO_OPP_ALERT_HOURS = 24

# ── Data Storage ─────────────────────────────────────────────────────────────
TRADES_FILE = "data/trades.json"
PERFORMANCE_FILE = "data/performance.csv"
