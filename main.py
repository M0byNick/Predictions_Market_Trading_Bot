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
import traceback
from datetime import datetime, timezone

from kalshi_client import KalshiClient
from kelly import kelly_size, category_bankroll, format_sizing_summary
from tracker import Tracker
from alerts import TelegramAlerter
from screeners.crypto import CryptoScreener
from screeners.weather import WeatherScreener
from screeners.economics import EconomicsScreener
import config


def run_screeners(client: KalshiClient, crypto: CryptoScreener,
                  weather: WeatherScreener, economics: EconomicsScreener) -> list:
    """Run all three screeners and collect opportunities."""
    all_opps = []

    print(f"\n{'═' * 60}")
    print(f"  Screening at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 60}")

    # Crypto screener — 50% allocation, highest expected edge
    print("\n₿  Scanning crypto markets...")
    try:
        crypto_opps = crypto.screen()
        print(f"   Found {len(crypto_opps)} opportunities")
        all_opps.extend(crypto_opps)
    except Exception as e:
        print(f"   Crypto screener error: {e}")
        traceback.print_exc()

    # Weather screener — 30% allocation, NWS-based edge
    print("\n🌡  Scanning weather markets...")
    try:
        weather_opps = weather.screen()
        print(f"   Found {len(weather_opps)} opportunities")
        all_opps.extend(weather_opps)
    except Exception as e:
        print(f"   Weather screener error: {e}")
        traceback.print_exc()

    # Economics screener — 20% allocation, conservative/manual review
    print("\n📈 Scanning economics markets...")
    try:
        econ_opps = economics.screen()
        print(f"   Found {len(econ_opps)} opportunities")
        all_opps.extend(econ_opps)
    except Exception as e:
        print(f"   Economics screener error: {e}")
        traceback.print_exc()

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

    # Calculate position sizing
    sizing = kelly_size(
        your_prob=opp["your_prob"],
        market_prob=opp["market_prob"],
        category_bankroll=cat_bankroll,
        side=opp["side"],
    )

    # If Kelly says no trade, skip silently
    if sizing["action"] in ("no_trade", "skip"):
        print(f"   ⏭  {opp['ticker']}: {sizing['reason']}")
        return False

    # Print the opportunity to console
    print(f"\n   {'─' * 50}")
    print(f"   📊 {opp['title']}")
    print(f"   Ticker: {opp['ticker']}")
    print(f"   Side: {opp['side'].upper()} | Edge: {opp['edge']:.1%}")
    print(f"   Size: {sizing['recommended_contracts']} contracts (${sizing['recommended_usd']})")
    print(f"   Rationale: {opp.get('rationale', 'N/A')}")
    print(f"   {'─' * 50}")

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
        print("   ⏳ Waiting for Telegram approval...")
        approved = alerter.wait_for_approval(timeout_seconds=300)

        if approved is None:
            alerter.send("⏰ Trade alert timed out. Skipping.")
            print("   ⏰ Timed out — skipping")
            return False
        elif not approved:
            alerter.send("❌ Trade rejected.")
            print("   ❌ Rejected")
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
        print(f"   ✅ Order placed: {order_result}")

        # Log the trade
        tracker.log_trade(
            ticker=opp["ticker"],
            category=category,
            side=opp["side"],
            your_prob=opp["your_prob"],
            market_prob=opp["market_prob"],
            num_contracts=sizing["recommended_contracts"],
            cost_usd=sizing["recommended_usd"],
            kelly_fraction=sizing["capped_fraction"],
            notes=opp.get("rationale", ""),
        )

        # Confirm via Telegram
        alerter.send_execution_confirmation(
            ticker=opp["ticker"],
            num_contracts=sizing["recommended_contracts"],
            cost_usd=sizing["recommended_usd"],
        )

        return True

    except Exception as e:
        error_msg = f"❌ Order failed for {opp['ticker']}: {e}"
        print(f"   {error_msg}")
        alerter.send(error_msg)
        return False


def main_loop():
    """Main screening and trading loop."""
    # Initialize components
    client = KalshiClient()
    tracker = Tracker()
    alerter = TelegramAlerter()

    crypto_screener = CryptoScreener(client)
    weather_screener = WeatherScreener(client)
    economics_screener = EconomicsScreener(client)

    print("🚀 Kalshi Bot starting...")
    print(f"   Bankroll: ${config.TOTAL_BANKROLL}")
    print(f"   Allocation: Crypto {config.ALLOCATION['crypto']:.0%} | "
          f"Weather {config.ALLOCATION['weather']:.0%} | "
          f"Economics {config.ALLOCATION['economics']:.0%}")
    print(f"   Kelly fraction: {config.KELLY_FRACTION:.0%}")
    print(f"   Min edge threshold: {config.MIN_EDGE_THRESHOLD:.0%}")
    print(f"   Approval required: {config.REQUIRE_APPROVAL}")
    print(f"   Screening every {config.SCREENER_INTERVAL_MINUTES} minutes")

    # Notify on Telegram that the bot is live
    alerter.send("🚀 <b>Kalshi Bot is live.</b>\nScreening crypto, weather, and economics markets.")

    while True:
        try:
            # Run all screeners
            opportunities = run_screeners(
                client, crypto_screener, weather_screener, economics_screener
            )

            if not opportunities:
                print("\n   No opportunities found this cycle.")
            else:
                print(f"\n   🎯 {len(opportunities)} opportunities found. Processing...")

                # Sort by edge size (highest first)
                opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)

                trades_executed = 0
                for opp in opportunities:
                    executed = process_opportunity(opp, alerter, tracker, client)
                    if executed:
                        trades_executed += 1

                print(f"\n   Executed {trades_executed}/{len(opportunities)} trades")

            # Sleep until next screening cycle
            print(f"\n   💤 Sleeping {config.SCREENER_INTERVAL_MINUTES} minutes...")
            time.sleep(config.SCREENER_INTERVAL_MINUTES * 60)

        except KeyboardInterrupt:
            print("\n\n🛑 Bot stopped by user.")
            alerter.send("🛑 Bot stopped.")
            break
        except Exception as e:
            print(f"\n❌ Error in main loop: {e}")
            traceback.print_exc()
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
    client = KalshiClient()
    crypto = CryptoScreener(client)
    weather = WeatherScreener(client)
    economics = EconomicsScreener(client)

    opportunities = run_screeners(client, crypto, weather, economics)

    if not opportunities:
        print("\nNo opportunities found.")
        return

    print(f"\n{'═' * 60}")
    print(f"  Found {len(opportunities)} opportunities (sorted by edge)")
    print(f"{'═' * 60}")

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
