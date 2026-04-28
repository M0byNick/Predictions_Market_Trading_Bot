import logging
import sqlite3
import time

from arb_bot.db import transaction
from arb_bot.signal.spread import ArbSignal

log = logging.getLogger(__name__)

# Paper-fill assumptions
SLIPPAGE_BPS = 30  # adverse slippage beyond quoted mid
FILL_PROBABILITY = 1.0  # paper v1: always fill; phase 2 can model queue position
CONTRACT_NOTIONAL_USD = 1.0  # Kalshi + Poly-US YES contracts settle 0/1 USD


def _fee_usd(price: float, units: int, fee_bps: int) -> float:
    return price * units * CONTRACT_NOTIONAL_USD * (fee_bps / 10_000.0)


def simulate_fill(
    conn: sqlite3.Connection, signal_id: int, sig: ArbSignal
) -> None:
    """Simulate both legs of the arb and record paper fills.

    paper v1 assumptions:
      - both legs fill instantly at mid +/- SLIPPAGE_BPS
      - no partial-fill modeling (phase 2)
      - fees applied per leg from arb_bot.signal.spread constants
    """
    from arb_bot.signal.spread import KALSHI_FEE_BPS, POLY_GLOBAL_FEE_BPS

    now_ts = int(time.time())
    slip = SLIPPAGE_BPS / 10_000.0

    if sig.direction == "buy_kalshi_yes_sell_poly_yes":
        kal_side, kal_price_intended = "buy", sig.kalshi_yes_mid
        kal_price_filled = sig.kalshi_yes_mid + slip
        poly_side, poly_price_intended = "sell", sig.poly_yes_mid
        poly_price_filled = sig.poly_yes_mid - slip
    elif sig.direction == "buy_poly_yes_sell_kalshi_yes":
        kal_side, kal_price_intended = "sell", sig.kalshi_yes_mid
        kal_price_filled = sig.kalshi_yes_mid - slip
        poly_side, poly_price_intended = "buy", sig.poly_yes_mid
        poly_price_filled = sig.poly_yes_mid + slip
    else:
        return

    with transaction(conn):
        conn.execute(
            """
            INSERT INTO paper_fills (signal_id, pair_id, leg, side, contract,
                price_intended, price_filled, size_filled, fees_usd, ts, state)
            VALUES (?, ?, 'kalshi', ?, 'yes', ?, ?, ?, ?, ?, 'filled')
            """,
            (
                signal_id,
                sig.pair_id,
                kal_side,
                kal_price_intended,
                kal_price_filled,
                sig.size_units,
                _fee_usd(kal_price_filled, sig.size_units, KALSHI_FEE_BPS),
                now_ts,
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_fills (signal_id, pair_id, leg, side, contract,
                price_intended, price_filled, size_filled, fees_usd, ts, state)
            VALUES (?, ?, 'poly_global', ?, 'yes', ?, ?, ?, ?, ?, 'filled')
            """,
            (
                signal_id,
                sig.pair_id,
                poly_side,
                poly_price_intended,
                poly_price_filled,
                sig.size_units,
                _fee_usd(poly_price_filled, sig.size_units, POLY_GLOBAL_FEE_BPS),
                now_ts,
            ),
        )
    log.info(
        "Paper fill: %s size=%d kal=%s@%.4f poly=%s@%.4f",
        sig.pair_id, sig.size_units, kal_side, kal_price_filled, poly_side, poly_price_filled,
    )
