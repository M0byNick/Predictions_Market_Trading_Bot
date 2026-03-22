"""
Main orchestrator for the Kalshi prediction market bot.

This is the entry point. It runs the three screeners on a configurable interval,
sends Telegram alerts for any opportunities found, waits for your approval (if
configured), executes trades, and logs everything to the tracker.

Usage:
    python main.py              # Normal mode: screen + alert + trade loop
    python main.py --backtest   # Review your historical performance
    python main.py --summary    # Print current performance summary
    python main.py --once       # Run screeners once and exit (good for testing)
"""
import sys
import time
from datetime import datetime, timezone

from kalshi_client import KalshiClient
from kelly import kelly_size, category_bankroll, format_sizing_summary
from tracker import Tracker
from alerts import TelegramAlerter
from screeners.crypto import CryptoScreener
from screeners.weather import WeatherScreener
from screeners.economics import EconomicsScreener
import config
from log import logger


def run_screeners(client: KalshiClient, crypto: CryptoScreener,
                  weather: WeatherScreener, economics: EconomicsScreener) -> list:
    """Run all three screeners and collect opportunities."""
    all_opps = []

    logger.info("Screening at %s", datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))

    # Crypto screener — 50% allocation, highest expected edge
    logger.info("Scanning crypto markets...")
    try:
        crypto_opps = crypto.screen()
        logger.info("Crypto: %d opportunities", len(crypto_opps))
        all_opps.extend(crypto_opps)
    except Exception:
        logger.error("Crypto screener error", exc_info=True)

    # Weather screener — 30% allocation, NWS-based edge
    logger.info("Scanning weather markets...")
    try:
        weather_opps = weather.screen()
        logger.info("Weather: %d opportunities", len(weather_opps))
        all_opps.extend(weather_opps)
    except Exception:
        logger.error("Weather screener error", exc_info=True)

    # Economics screener — 20% allocation, conservative/manual review
    logger.info("Scanning economics markets...")
    try:
        econ_opps = economics.screen()
        logger.info("Economics: %d opportunities", len(econ_opps))
        all_opps.extend(econ_opps)
    except Exception:
        logger.error("Economics screener error", exc_info=True)

    return all_opps


def process_opportunity(opp: dict, alerter: TelegramAlerter,
                        tracker: Tracker, client: KalshiClient) -> bool:
    """
    Process a single trade opportunity:
      1. Calculate Kelly-optimal position size
      2. Send Telegram alert with details
      3. Wait for approval (if configured)
      4. Execute trade and log it

    Returns True if a trade was executed, False otherwise.
    """
    category = opp["category"]
    cat_bankroll = category_bankroll(category)

    # Economics uses a reduced Kelly fraction due to heuristic edge estimates
    kelly_ovr = config.ECON_MAX_KELLY_FRACTION if category == "economics" else None

    # Calculate position sizing
    sizing = kelly_size(
        your_prob=opp["your_prob"],
        market_prob=opp["market_prob"],
        category_bankroll=cat_bankroll,
        side=opp["side"],
        kelly_override=kelly_ovr,
    )

    # If Kelly says no trade, skip silently
    if sizing["action"] in ("no_trade", "skip"):
        logger.debug("Skip %s: %s", opp['ticker'], sizing['reason'])
        return False

    # Economics alert-only mode: notify but do not execute
    if category == "economics" and config.ECON_ALERT_ONLY:
        logger.info("[ALERT ONLY] %s | %s %s | Edge: %.1f%%",
                     opp['title'], opp['side'].upper(), opp['ticker'], opp['edge'] * 100)
        alerter.send(
            f"📊 <b>[ALERT ONLY — Economics]</b>\n"
            f"{opp.get('title', opp['ticker'])}\n"
            f"Side: {opp['side'].upper()} | Edge: {opp['edge']:.1%}\n"
            f"Suggested: {sizing['recommended_contracts']} contracts (${sizing['recommended_usd']})\n"
            f"{opp.get('rationale', '')}\n"
            f"⚠️ Not auto-executing — set ECON_ALERT_ONLY=False to enable."
        )
        return False

    # Log the opportunity
    logger.info("%s | %s %s | Edge: %.1f%% | %d contracts ($%s)",
                opp['title'], opp['side'].upper(), opp['ticker'],
                opp['edge'] * 100, sizing['recommended_contracts'], sizing['recommended_usd'])

    # Send Telegram alert
    alerter.send_trade_alert(
        ticker=opp["ticker"],
        category=category,
        side=opp["side"],
        sizing=sizing,
        market_title=opp.get("title", ""),
        rationale=opp.get("rationale", ""),
    )

    # Wait for approval if required
    if config.REQUIRE_APPROVAL:
        logger.info("Waiting for Telegram approval...")
        approved = alerter.wait_for_approval(timeout_seconds=300)

        if approved is None:
            alerter.send("⏰ Trade alert timed out. Skipping.")
            logger.info("Approval timed out for %s", opp['ticker'])
            return False
        elif not approved:
            alerter.send("❌ Trade rejected.")
            logger.info("Trade rejected for %s", opp['ticker'])
            return False

    # Execute the trade
    try:
        price_cents = int(opp["market_prob"] * 100)
        order_result = client.place_order(
            ticker=opp["ticker"],
            side=opp["side"],
            size=sizing["recommended_contracts"],
            order_type=config.DEFAULT_ORDER_TYPE,
            price=price_cents,
        )
        logger.info("Order placed: %s", order_result)

        # Check fill status
        actual_contracts = sizing["recommended_contracts"]
        actual_cost = sizing["recommended_usd"]
        order_id = order_result.get("order", {}).get("order_id")

        if order_id:
            try:
                order_status = client.get_order(order_id)
                order_data = order_status.get("order", order_status)
                status = order_data.get("status", "unknown")
                remaining = order_data.get("remaining_count", 0)
                filled = actual_contracts - remaining

                if status == "canceled" or filled == 0:
                    msg = f"Order {order_id} was not filled (status: {status})"
                    logger.warning(msg)
                    alerter.send(f"⚠️ {msg}")
                    return False

                if filled < actual_contracts:
                    actual_contracts = filled
                    actual_cost = round(actual_cost * (filled / sizing["recommended_contracts"]), 2)
                    msg = f"Partial fill: {filled}/{sizing['recommended_contracts']} contracts"
                    logger.warning(msg)
                    alerter.send(f"⚠️ {msg}")
            except Exception as e:
                logger.warning("Fill check failed (logging requested qty): %s", e)

        # Log the trade with actual filled quantities
        tracker.log_trade(
            ticker=opp["ticker"],
            category=category,
            side=opp["side"],
            your_prob=opp["your_prob"],
            market_prob=opp["market_prob"],
            num_contracts=actual_contracts,
            cost_usd=actual_cost,
            kelly_fraction=sizing["capped_fraction"],
            notes=opp.get("rationale", ""),
        )

        # Confirm via Telegram
        alerter.send_execution_confirmation(
            ticker=opp["ticker"],
            num_contracts=actual_contracts,
            cost_usd=actual_cost,
        )

        return True

    except Exception as e:
        logger.error("Order failed for %s: %s", opp['ticker'], e, exc_info=True)
        alerter.send(f"❌ Order failed for {opp['ticker']}: {e}")
        return False


def _make_client():
    """Create the appropriate client based on PAPER_TRADING config."""
    if config.PAPER_TRADING:
        from paper_client import PaperClient
        return PaperClient()
    return KalshiClient()


def main_loop():
    """Main screening and trading loop."""
    # Initialize components
    client = _make_client()
    tracker = Tracker()
    alerter = TelegramAlerter()

    crypto_screener = CryptoScreener(client)
    weather_screener = WeatherScreener(client)
    economics_screener = EconomicsScreener(client)

    logger.info("Kalshi Bot starting | Bankroll: $%d | Kelly: %.0f%% | Min edge: %.0f%% | Approval: %s | Interval: %dm",
                config.TOTAL_BANKROLL, config.KELLY_FRACTION * 100,
                config.MIN_EDGE_THRESHOLD * 100, config.REQUIRE_APPROVAL,
                config.SCREENER_INTERVAL_MINUTES)

    # Notify on Telegram that the bot is live
    alerter.send("🚀 <b>Kalshi Bot is live.</b>\nScreening crypto, weather, and economics markets.")

    while True:
        try:
            # Run all screeners
            opportunities = run_screeners(
                client, crypto_screener, weather_screener, economics_screener
            )

            if not opportunities:
                logger.info("No opportunities found this cycle.")
            else:
                logger.info("%d opportunities found. Processing...", len(opportunities))

                # Sort by edge size (highest first)
                opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)

                trades_executed = 0
                for opp in opportunities:
                    executed = process_opportunity(opp, alerter, tracker, client)
                    if executed:
                        trades_executed += 1

                logger.info("Executed %d/%d trades", trades_executed, len(opportunities))

            # Sleep until next screening cycle
            logger.info("Sleeping %d minutes...", config.SCREENER_INTERVAL_MINUTES)
            time.sleep(config.SCREENER_INTERVAL_MINUTES * 60)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            alerter.send("🛑 Bot stopped.")
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e, exc_info=True)
            alerter.send(f"⚠️ Bot error: {e}")
            time.sleep(60)  # Brief pause before retrying


def show_summary():
    """Print performance summary from tracked trades."""
    tracker = Tracker()
    print(tracker.summary())

    # Also export to CSV for spreadsheet analysis
    tracker.export_csv()
    print(f"\nTrade data exported to {config.PERFORMANCE_FILE}")


def run_once():
    """Run screeners once and print results (no trading). Good for testing."""
    client = _make_client()
    crypto = CryptoScreener(client)
    weather = WeatherScreener(client)
    economics = EconomicsScreener(client)

    opportunities = run_screeners(client, crypto, weather, economics)

    if not opportunities:
        logger.info("No opportunities found.")
        return

    logger.info("Found %d opportunities (sorted by edge)", len(opportunities))

    opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)

    for i, opp in enumerate(opportunities, 1):
        cat_bankroll_val = category_bankroll(opp["category"])
        sizing = kelly_size(opp["your_prob"], opp["market_prob"],
                            cat_bankroll_val, opp["side"])
        print(f"\n{i}. [{opp['category'].upper()}] {opp['title']}")
        print(f"   Ticker: {opp['ticker']}")
        print(f"   Side: {opp['side'].upper()} | Edge: {opp['edge']:.1%}")
        print(f"   {format_sizing_summary(sizing)}")
        print(f"   Rationale: {opp.get('rationale', 'N/A')}")


if __name__ == "__main__":
    if "--summary" in sys.argv or "--backtest" in sys.argv:
        show_summary()
    elif "--once" in sys.argv:
        run_once()
    else:
        main_loop()
