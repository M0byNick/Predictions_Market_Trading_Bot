"""Shared utilities for screeners."""
from __future__ import annotations


def get_market_prob(market: dict) -> float:
    """
    Extract the market probability (0-1) from a Kalshi market dict.

    The API returns dollar-denominated fields (e.g., 'last_price_dollars': '0.6500')
    as well as legacy cent-based fields. This handles both formats.
    """
    # Try dollar-denominated fields first (current API format)
    for field in ("last_price_dollars", "yes_ask_dollars", "yes_bid_dollars"):
        val = market.get(field)
        if val is not None:
            try:
                prob = float(val)
                if 0 < prob < 1:
                    return prob
            except (ValueError, TypeError):
                continue

    # Fall back to legacy cent-based fields
    for field in ("last_price", "yes_ask", "yes_bid"):
        val = market.get(field)
        if val is not None and val > 0:
            return val / 100.0

    return 0.5  # Default if nothing found
