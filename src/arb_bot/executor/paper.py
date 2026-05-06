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


def _fee_usd_kalshi(price: float, units: int) -> float:
    """Per-leg Kalshi fee in dollars (per-trade formula, not flat bps)."""
    from arb_bot.signal.spread import _kalshi_taker_fee_per_contract
    return _kalshi_taker_fee_per_contract(price) * units


def _fee_usd_poly(price: float, units: int) -> float:
    """Per-leg Polymarket fee in dollars (per-trade formula)."""
    from arb_bot.signal.spread import _poly_taker_fee_per_contract
    return _poly_taker_fee_per_contract(price) * units


def simulate_fill(
    conn: sqlite3.Connection, signal_id: int, sig: ArbSignal
) -> None:
    """Simulate both legs of the arb and record paper fills.

    paper v1 assumptions:
      - both legs fill at mid +/- SLIPPAGE_BPS (paper proxy for real fill)
      - fees applied per leg using the per-trade Kalshi/Polymarket formulas
        from signal.spread (matches the audit-gap-fix fee model)
      - no partial-fill modeling (phase 2)
    """
    now_ts = int(time.time())
    slip = SLIPPAGE_BPS / 10_000.0

    # Same-polarity arbs: one buy + one sell, prices anchored to each leg's mid
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
    # Inverse-polarity arbs: BOTH buy or BOTH sell. Slippage adverse on
    # whichever side we're crossing (buy → above mid; sell → below mid).
    elif sig.direction == "sell_both_yes_inverse":
        # sum_YES > $1, capture by selling both YES legs
        kal_side, kal_price_intended = "sell", sig.kalshi_yes_mid
        kal_price_filled = sig.kalshi_yes_mid - slip
        poly_side, poly_price_intended = "sell", sig.poly_yes_mid
        poly_price_filled = sig.poly_yes_mid - slip
    elif sig.direction == "buy_both_yes_inverse":
        # sum_YES < $1, capture by buying both YES legs
        kal_side, kal_price_intended = "buy", sig.kalshi_yes_mid
        kal_price_filled = sig.kalshi_yes_mid + slip
        poly_side, poly_price_intended = "buy", sig.poly_yes_mid
        poly_price_filled = sig.poly_yes_mid + slip
    else:
        # flat / skip_polarity_unknown / anything we don't know how to fill
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
                _fee_usd_kalshi(kal_price_filled, sig.size_units),
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
                _fee_usd_poly(poly_price_filled, sig.size_units),
                now_ts,
            ),
        )
    log.info(
        "Paper fill: %s size=%d kal=%s@%.4f poly=%s@%.4f",
        sig.pair_id, sig.size_units, kal_side, kal_price_filled, poly_side, poly_price_filled,
    )
