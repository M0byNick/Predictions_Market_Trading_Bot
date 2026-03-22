"""
Kelly criterion bet sizing for prediction market contracts.

The Kelly criterion tells you the optimal fraction of your bankroll to wager
given your edge and the odds. Full Kelly maximizes long-run growth rate but
has extreme variance. We use fractional Kelly (default 1/4) for a smoother ride.

For a binary Kalshi contract priced at `market_prob`:
  - You pay `market_prob` per contract if buying YES (or `1 - market_prob` for NO)
  - You receive $1 if correct, $0 if wrong
  - Your edge = your_prob - market_prob

Kelly fraction for a binary bet:
  f* = (p * b - q) / b
  where p = your estimated probability, q = 1 - p, b = net odds = (1/market_prob) - 1

This simplifies to:
  f* = (your_prob - market_prob) / (1 - market_prob)   [for YES bets]
  f* = (your_no_prob - market_no_prob) / (1 - market_no_prob)  [for NO bets]
"""
import config


def kelly_size(your_prob: float, market_prob: float,
               category_bankroll: float, side: str = "yes",
               kelly_override: float = None) -> dict:
    """
    Calculate the optimal position size for a Kalshi contract.

    Args:
        your_prob: Your estimated probability the event happens (0.0 to 1.0).
        market_prob: Current market price as a probability (0.0 to 1.0).
                     For a YES contract priced at 62 cents, this is 0.62.
        category_bankroll: The bankroll allocated to this category (in USD).
        side: 'yes' or 'no' — which side of the contract you're buying.

    Returns:
        Dict with sizing details including recommended contracts and rationale.
    """
    # Flip probabilities for NO side bets.
    # If you think the event WON'T happen, you're buying NO,
    # so your edge is on the (1 - prob) side.
    if side == "no":
        your_prob = 1 - your_prob
        market_prob = 1 - market_prob

    edge = your_prob - market_prob

    # No edge = no trade. Don't donate to the market.
    if edge <= 0:
        return {
            "action": "no_trade",
            "reason": "No positive edge detected",
            "edge": edge,
            "kelly_fraction": 0,
            "recommended_contracts": 0,
            "recommended_usd": 0,
        }

    # Check minimum edge threshold from config
    if edge < config.MIN_EDGE_THRESHOLD:
        return {
            "action": "no_trade",
            "reason": f"Edge {edge:.1%} below minimum threshold {config.MIN_EDGE_THRESHOLD:.1%}",
            "edge": edge,
            "kelly_fraction": 0,
            "recommended_contracts": 0,
            "recommended_usd": 0,
        }

    # Kelly formula for binary bets:
    # f* = (p * (1/cost) - (1-p) * (1/cost_if_wrong)) simplified to:
    # f* = edge / (1 - market_prob)
    # This gives the fraction of bankroll to risk.
    full_kelly = edge / (1 - market_prob) if market_prob < 1.0 else 0

    # Apply fractional Kelly — trade a fraction of the theoretical optimum
    kelly_frac = kelly_override if kelly_override is not None else config.KELLY_FRACTION
    fractional_kelly = full_kelly * kelly_frac

    # Safety cap: never risk more than MAX_BET_FRACTION of category bankroll
    capped_fraction = min(fractional_kelly, config.MAX_BET_FRACTION)

    # Convert to dollars. Each YES contract costs `market_prob * 100` cents.
    # The bankroll fraction tells us total dollars to allocate.
    dollars_to_risk = capped_fraction * category_bankroll

    # Each contract costs market_prob dollars (since prices are in cents, 1-99)
    cost_per_contract = market_prob  # in dollar terms (e.g., 0.62 = 62 cents)
    num_contracts = int(dollars_to_risk / cost_per_contract) if cost_per_contract > 0 else 0

    # Don't place trades for less than $5 — not worth the attention
    if num_contracts * cost_per_contract < 5.0:
        return {
            "action": "skip",
            "reason": "Position size too small (< $5) to be worth executing",
            "edge": edge,
            "kelly_fraction": capped_fraction,
            "recommended_contracts": num_contracts,
            "recommended_usd": round(num_contracts * cost_per_contract, 2),
        }

    return {
        "action": "trade",
        "side": side,
        "edge": round(edge, 4),
        "your_prob": round(your_prob, 4),
        "market_prob": round(market_prob, 4),
        "full_kelly_fraction": round(full_kelly, 4),
        "fractional_kelly": round(fractional_kelly, 4),
        "capped_fraction": round(capped_fraction, 4),
        "recommended_contracts": num_contracts,
        "recommended_usd": round(num_contracts * cost_per_contract, 2),
        "category_bankroll": round(category_bankroll, 2),
        "expected_value": round(num_contracts * edge, 2),
    }


def category_bankroll(category: str, total_bankroll: float = None) -> float:
    """
    Calculate the bankroll allocated to a specific category based on
    the allocation percentages in config.py.
    """
    bankroll = total_bankroll or config.TOTAL_BANKROLL
    allocation = config.ALLOCATION.get(category, 0)
    return bankroll * allocation


def format_sizing_summary(sizing: dict) -> str:
    """Format a sizing result into a human-readable string for Telegram alerts."""
    if sizing["action"] in ("no_trade", "skip"):
        return f"⏭ {sizing['reason']} (edge: {sizing.get('edge', 0):.1%})"

    return (
        f"📊 Trade Signal\n"
        f"Side: {sizing['side'].upper()}\n"
        f"Your prob: {sizing['your_prob']:.1%} vs Market: {sizing['market_prob']:.1%}\n"
        f"Edge: {sizing['edge']:.1%}\n"
        f"Kelly: {sizing['capped_fraction']:.1%} of bankroll\n"
        f"Size: {sizing['recommended_contracts']} contracts (${sizing['recommended_usd']})\n"
        f"Expected value: ${sizing['expected_value']}"
    )
