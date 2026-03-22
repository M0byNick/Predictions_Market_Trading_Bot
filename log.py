"""
Centralized logging for the trading bot.

Usage:
    from log import logger
    logger.info("Screening crypto markets")
    logger.warning("CoinGecko timeout")
    logger.error("Order placement failed", exc_info=True)

Outputs to both console and data/bot.log (rotating, 5 x 5MB).
"""
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "data"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("trading_bot")
logger.setLevel(logging.DEBUG)

# Console: INFO and above, concise format
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
))

# File: DEBUG and above, full format with timestamps, rotating
_file = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
_file.setLevel(logging.DEBUG)
_file.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logger.addHandler(_console)
logger.addHandler(_file)
