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
    python main.py --calibration  # Print calibration report
    python main.py --dry-run    # Run one full cycle and exit (CI-friendly)
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

    cycle_id = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    logger.info("Screening at %s", cycle_id)

    # Crypto screener — 50% allocation, highest expected edge
    logger.info("Scanning crypto markets...")
    try:
        crypto_opps = crypto.screen(cycle_id=cycle_id)
        logger.info("Crypto: %d opportunities", len(crypto_opps))
        all_opps.extend(crypto_opps)
    except Exception:
        logger.error("Crypto screener error", exc_info=True)

    # Weather screener — 30% allocation, NWS-based edge
    logger.info("Scanning weather markets...")
    try:
        weather_opps = weather.screen(cycle_id=cycle_id)
        logger.info("Weather: %d opportunities", len(weather_opps))
        all_opps.extend(weather_opps)
    except Exception:
        logger.error("Weather screener error", exc_info=True)

    # Economics screener — 20% allocation, conservative/manual review
    logger.info("Scanning economics markets...")
    try:
        econ_opps = economics.screen(cycle_id=cycle_id)
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
    edge = opp.get("edge", 0)
    market_prob = opp.get("market_prob", 0.5)
    side = opp.get("side", "yes")

    # ── Overconfidence guardrail ──────────────────────────────────────
    # Reject trades where the claimed edge is implausibly large.
    # A 50%+ edge almost always means the model is wrong, not that
    # we found a 50¢ bill on the ground. Real edges are 5-30%.
    if edge > config.MAX_EDGE_THRESHOLD:
        logger.info("GUARDRAIL: Skip %s — edge %.1f%% exceeds max %.0f%%",
                     opp['ticker'], edge * 100, config.MAX_EDGE_THRESHOLD * 100)
        tracker.log_skipped(opp['ticker'], category, side,
                            opp.get('your_prob', 0), market_prob, edge, "edge_cap")
        return False

    # ── Penny market filter ───────────────────────────────────────────
    if market_prob <= config.MIN_MARKET_PRICE:
        logger.info("GUARDRAIL: Skip %s — market price %.0f¢ below %.0f¢ floor",
                     opp['ticker'], market_prob * 100, config.MIN_MARKET_PRICE * 100)
        tracker.log_skipped(opp['ticker'], category, side,
                            opp.get('your_prob', 0), market_prob, edge, "penny_floor")
        return False

    # ── Per-ticker position limit ─────────────────────────────────────
    existing = tracker.get_open_positions_for_ticker(opp["ticker"])
    if existing > 0:
        logger.debug("Skip %s — already have %d open position(s)", opp['ticker'], existing)
        tracker.log_skipped(opp['ticker'], category, side,
                            opp.get('your_prob', 0), market_prob, edge, "per_ticker_limit")
        return False

    cat_bankroll = category_bankroll(category)

    # Economics uses a reduced Kelly fraction due to model-based edge
    kelly_ovr = config.ECON_MAX_KELLY_FRACTION if category == "economics" else None

    # Calculate position sizing
    sizing = kelly_size(
        your_prob=opp["your_prob"],
        market_prob=opp["market_prob"],
        category_bankroll=cat_bankroll,
        side=opp["side"],
        kelly_override=kelly_ovr,
    )

    # If Kelly says no trade, skip
    if sizing["action"] in ("no_trade", "skip"):
        logger.debug("Skip %s: %s", opp['ticker'], sizing['reason'])
        tracker.log_skipped(opp['ticker'], category, side,
                            opp.get('your_prob', 0), market_prob, edge, "kelly_no_trade")
        return False

    # Signal-only mode: alert but do not execute anything
    if config.SIGNAL_ONLY:
        logger.info("[SIGNAL ONLY] %s | %s %s | Edge: %.1f%%",
                     opp['title'], opp['side'].upper(), opp['ticker'], opp['edge'] * 100)
        alerter.send(
            f"📡 <b>[SIGNAL ONLY]</b>\n"
            f"{opp.get('title', opp['ticker'])}\n"
            f"Side: {opp['side'].upper()} | Edge: {opp['edge']:.1%}\n"
            f"Suggested: {sizing['recommended_contracts']} contracts (${sizing['recommended_usd']})\n"
            f"{opp.get('rationale', '')}"
        )
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

        # Mark as pending before placing (crash recovery)
        tracker.mark_pending(
            ticker=opp["ticker"], side=opp["side"],
            contracts=sizing["recommended_contracts"],
            cost_usd=sizing["recommended_usd"],
        )

        order_result = client.place_order(
            ticker=opp["ticker"],
            side=opp["side"],
            size=sizing["recommended_contracts"],
            order_type=config.DEFAULT_ORDER_TYPE,
            price=price_cents,
        )
        logger.info("Order placed: %s", order_result)

        # Update pending record with actual order_id
        order_id = order_result.get("order", {}).get("order_id")
        if order_id:
            tracker.clear_pending(opp["ticker"], opp["side"])
            tracker.mark_pending(
                ticker=opp["ticker"], side=opp["side"],
                contracts=sizing["recommended_contracts"],
                cost_usd=sizing["recommended_usd"],
                order_id=order_id,
            )

        # Check fill status
        actual_contracts = sizing["recommended_contracts"]
        actual_cost = sizing["recommended_usd"]

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

        # Compute Kelly sizing details for analysis
        full_kelly_val = sizing.get("full_kelly_fraction", 0)
        frac_kelly_val = sizing.get("fractional_kelly", 0)
        kelly_rec_usd_val = round(frac_kelly_val * sizing.get("category_bankroll", 0), 2)
        kelly_mult = round(actual_cost / kelly_rec_usd_val, 4) if kelly_rec_usd_val > 0 else 0

        # Log the trade with actual filled quantities + entry timing
        entry_hour = datetime.now(timezone.utc).hour
        entry_vol = opp.get("vol") or opp.get("entry_vol")
        entry_fgi = opp.get("fgi") or opp.get("entry_fgi")

        tracker.log_trade(
            ticker=opp["ticker"],
            category=category,
            side=opp["side"],
            your_prob=opp["your_prob"],
            market_prob=opp["market_prob"],
            num_contracts=actual_contracts,
            cost_usd=actual_cost,
            kelly_fraction=sizing["capped_fraction"],
            full_kelly=full_kelly_val,
            fractional_kelly=frac_kelly_val,
            kelly_rec_usd=kelly_rec_usd_val,
            kelly_multiplier=kelly_mult,
            notes=opp.get("rationale", ""),
            entry_hour=entry_hour,
            entry_vol=entry_vol,
            entry_fgi=entry_fgi,
        )

        # Trade is tracked — remove from pending orders
        tracker.clear_pending(opp["ticker"], opp["side"])

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
        tracker.clear_pending(opp["ticker"], opp["side"])
        return False


def check_open_prices(tracker: Tracker, client: KalshiClient) -> None:
    """
    Re-price all open positions to track post-entry market movement.
    Runs once per cycle. Stores price deltas in price_checks table.
    """
    from screeners.utils import get_market_prob

    rows = tracker.conn.execute(
        "SELECT id, ticker, market_prob FROM trades WHERE outcome IS NULL"
    ).fetchall()

    if not rows:
        return

    # Group by ticker to minimize API calls
    ticker_trades = {}
    for r in rows:
        ticker_trades.setdefault(r["ticker"], []).append(dict(r))

    checked = 0
    for ticker, trades in ticker_trades.items():
        try:
            market_data = client.get_market(ticker)
            market = market_data.get("market", market_data)
            current_price = get_market_prob(market)
            if current_price is None or current_price <= 0:
                continue

            for trade in trades:
                tracker.log_price_check(
                    trade["id"], ticker, current_price, trade["market_prob"]
                )
                checked += 1
        except Exception:
            continue

    if checked:
        logger.info("Price check: tracked %d open positions", checked)


def _log_weather_actual(tracker: Tracker, ticker: str, trade: dict) -> None:
    """Log actual vs forecast weather data when a weather trade settles."""
    from screeners.weather import WeatherScreener
    import json

    # Determine city from ticker (e.g., KXHIGHCHI-26MAR24-B52.5 → CHI)
    city = None
    for c in ["NYC", "CHI", "MIA", "AUS"]:
        city_code = {"NYC": "NY", "CHI": "CHI", "MIA": "MIA", "AUS": "AUS"}[c]
        if city_code in ticker.upper():
            city = c
            break

    if not city:
        return

    actual = WeatherScreener.get_actual_temp(city)
    if actual is None:
        return

    # Try to find the forecast from snapshots
    snap_row = tracker.conn.execute("""
        SELECT data FROM market_snapshots
        WHERE ticker = ? AND category = 'weather'
        ORDER BY timestamp DESC LIMIT 1
    """, (ticker,)).fetchone()

    forecast_high = None
    forecast_std = None
    sigma_source = None
    if snap_row:
        try:
            snap = json.loads(snap_row["data"])
            forecast_high = snap.get("forecast_high")
            forecast_std = snap.get("forecast_std")
            sigma_source = snap.get("sigma_source")
        except (json.JSONDecodeError, TypeError):
            pass

    error = round(actual - forecast_high, 1) if forecast_high is not None else None
    date_str = ticker.split("-")[1][:7] if "-" in ticker else ""

    try:
        tracker.conn.execute("""
            INSERT OR IGNORE INTO weather_actuals
                (date, city, actual_high_f, forecast_high_f, forecast_std,
                 sigma_source, error_f)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (date_str, city, actual, forecast_high, forecast_std,
              sigma_source, error))
        tracker.conn.commit()
        if error is not None:
            logger.info("Weather actual: %s %s forecast=%.0f°F actual=%.0f°F error=%+.1f°F",
                        city, date_str, forecast_high, actual, error)
    except Exception as e:
        logger.warning("Failed to log weather actual: %s", e)


def check_settlements(tracker: Tracker, client: KalshiClient,
                      alerter: TelegramAlerter) -> int:
    """
    Check all open trades for settlements. For each unique ticker with open
    trades, query the Kalshi API to see if the market has settled. If so,
    determine win/loss and update the tracker + paper client.

    Returns the number of trades settled this cycle.
    """
    from paper_client import PaperClient

    # Get all open trades (outcome is NULL)
    rows = tracker.conn.execute(
        "SELECT id, ticker, side, category FROM trades WHERE outcome IS NULL"
    ).fetchall()
    open_trades = [dict(r) for r in rows]

    if not open_trades:
        return 0

    # Group by ticker to minimize API calls
    ticker_trades = {}
    for trade in open_trades:
        ticker_trades.setdefault(trade["ticker"], []).append(trade)

    settled_count = 0
    for ticker, trades in ticker_trades.items():
        try:
            market_data = client.get_market(ticker)
            market = market_data.get("market", market_data)
            status = market.get("status", "")

            if status not in ("settled", "finalized"):
                continue

            # Determine the actual result
            # Kalshi settled markets have a "result" field: "yes" or "no"
            result = market.get("result", "")
            if result not in ("yes", "no"):
                # Some markets use settlement_value instead
                settlement_val = market.get("settlement_value")
                if settlement_val is not None:
                    result = "yes" if settlement_val > 0 else "no"
                else:
                    logger.warning("Settled market %s has no result field: %s", ticker, market)
                    continue

            settlement_price = 1.0 if result == "yes" else 0.0

            # Backfill skipped opportunities with this ticker's result
            tracker.backfill_skipped_settlements(ticker, result)

            # Log actual weather data for weather contracts
            if trades and trades[0].get("category") == "weather":
                _log_weather_actual(tracker, ticker, trades[0])

            for trade in trades:
                won = (trade["side"] == result)
                outcome = "win" if won else "loss"

                tracker.record_outcome(trade["id"], outcome, settlement_price)

                # Settle paper position too
                if isinstance(client, PaperClient):
                    client.settle_position(ticker, trade["side"], result)

                # Get the updated trade for P&L
                updated = tracker.conn.execute(
                    "SELECT pnl_usd FROM trades WHERE id = ?", (trade["id"],)
                ).fetchone()
                pnl = updated["pnl_usd"] if updated else 0.0

                logger.info("Settled: %s %s → %s (P&L: $%.2f)",
                            trade["side"].upper(), ticker, outcome.upper(), pnl)
                alerter.send_settlement(ticker, outcome, pnl)
                settled_count += 1

        except Exception as e:
            logger.warning("Settlement check failed for %s: %s", ticker, e)
            continue

    if settled_count > 0:
        logger.info("Settled %d trade(s) this cycle", settled_count)

    return settled_count


def reconcile_pending_orders(tracker: Tracker, client: KalshiClient,
                             alerter: TelegramAlerter) -> None:
    """Check for orphaned orders from a previous crash and reconcile them."""
    pending = tracker.get_pending_orders()
    if not pending:
        return

    logger.warning("Found %d pending orders from previous run — reconciling...", len(pending))
    alerter.send(f"⚠️ Reconciling {len(pending)} pending order(s) from previous run...")

    for p in pending:
        order_id = p.get("order_id")
        ticker = p["ticker"]
        side = p["side"]

        if order_id:
            try:
                order_status = client.get_order(order_id)
                order_data = order_status.get("order", order_status)
                status = order_data.get("status", "unknown")
                remaining = order_data.get("remaining_count", 0)
                filled = p["contracts"] - remaining

                if status == "filled" or filled > 0:
                    actual_cost = round(p["cost_usd"] * (filled / p["contracts"]), 2) if p["contracts"] else 0
                    tracker.log_trade(
                        ticker=ticker, category="unknown", side=side,
                        your_prob=0, market_prob=0,
                        num_contracts=filled, cost_usd=actual_cost,
                        kelly_fraction=0, notes=f"Recovered from crash (order {order_id})",
                    )
                    msg = f"Recovered order {order_id}: {filled} contracts filled on {ticker}"
                    logger.info(msg)
                    alerter.send(f"🔄 {msg}")
                else:
                    msg = f"Pending order {order_id} on {ticker}: status={status}, no fills"
                    logger.warning(msg)
                    alerter.send(f"⚠️ {msg}")
            except Exception as e:
                logger.error("Failed to reconcile order %s: %s", order_id, e)
                alerter.send(f"❌ Could not reconcile order {order_id} on {ticker}: {e}")
        else:
            logger.warning("Pending order for %s has no order_id — may have crashed before placement", ticker)

        tracker.clear_pending(ticker, side)


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

    # Reconcile any orphaned orders from a previous crash
    reconcile_pending_orders(tracker, client, alerter)

    # Notify on Telegram that the bot is live
    alerter.send("🚀 <b>Kalshi Bot is live.</b>\nScreening crypto, weather, and economics markets.")

    # Health check state
    start_time = datetime.now(timezone.utc)
    cycle_count = 0
    last_opp_time = "never"
    no_opp_alert_sent = False

    while True:
        try:
            cycle_count += 1

            # Check for settled contracts and update tracker
            try:
                open_count = tracker.conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE outcome IS NULL"
                ).fetchone()[0]
                logger.info("Settlement check: %d open trades to scan", open_count)
                settled = check_settlements(tracker, client, alerter)
                if settled:
                    logger.info("Settlement check: %d trade(s) resolved", settled)

                # Post-entry price tracking on remaining open positions
                check_open_prices(tracker, client)
            except Exception as e:
                logger.warning("Settlement check error: %s", e)

            # Run all screeners
            opportunities = run_screeners(
                client, crypto_screener, weather_screener, economics_screener
            )

            if not opportunities:
                logger.info("No opportunities found this cycle.")
            else:
                last_opp_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                no_opp_alert_sent = False
                logger.info("%d opportunities found. Processing...", len(opportunities))

                # Sort by edge size (highest first)
                opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)

                trades_executed = 0
                for opp in opportunities:
                    executed = process_opportunity(opp, alerter, tracker, client)
                    if executed:
                        trades_executed += 1

                logger.info("Executed %d/%d trades", trades_executed, len(opportunities))

            # Health check heartbeat
            if cycle_count % config.HEALTH_CHECK_INTERVAL_CYCLES == 0:
                uptime_h = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600
                try:
                    balance = client.get_balance().get("balance", 0) / 100
                except Exception:
                    balance = 0.0
                alerter.send_health_check(cycle_count, uptime_h, last_opp_time, balance)

            # No-opportunity watchdog (alert once, not every cycle)
            if last_opp_time == "never" or not no_opp_alert_sent:
                elapsed_h = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600
                if elapsed_h >= config.NO_OPP_ALERT_HOURS and last_opp_time == "never":
                    alerter.send_no_opportunity_alert(elapsed_h)
                    no_opp_alert_sent = True

            # Poll for Telegram commands during sleep
            # Check every 10s so commands get a response within seconds
            logger.info("Sleeping %d minutes (polling for Telegram commands)...",
                         config.SCREENER_INTERVAL_MINUTES)
            sleep_seconds = config.SCREENER_INTERVAL_MINUTES * 60
            poll_interval = 10
            slept = 0
            while slept < sleep_seconds:
                alerter.check_commands(tracker=tracker, client=client)
                time.sleep(poll_interval)
                slept += poll_interval

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


def show_calibration():
    """Print calibration analysis report."""
    from calibration import CalibrationAnalyzer
    analyzer = CalibrationAnalyzer()

    category = None
    if "--category" in sys.argv:
        idx = sys.argv.index("--category")
        if idx + 1 < len(sys.argv):
            category = sys.argv[idx + 1]

    print(analyzer.full_report(category))

    if "--export" in sys.argv:
        path = analyzer.export_csv(category=category)
        print(f"Calibration data exported to {path}")


def dry_run():
    """Run one full cycle (screen + size + paper-execute) and exit. CI-friendly."""
    client = _make_client()
    tracker = Tracker()
    alerter = TelegramAlerter()

    crypto = CryptoScreener(client)
    weather = WeatherScreener(client)
    economics = EconomicsScreener(client)

    opportunities = run_screeners(client, crypto, weather, economics)

    if not opportunities:
        logger.info("Dry run: no opportunities found.")
        return

    opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)
    trades_executed = 0
    for opp in opportunities:
        executed = process_opportunity(opp, alerter, tracker, client)
        if executed:
            trades_executed += 1

    logger.info("Dry run complete: %d/%d trades executed", trades_executed, len(opportunities))

    # Print summary
    print(tracker.summary())


if __name__ == "__main__":
    if "--summary" in sys.argv or "--backtest" in sys.argv:
        show_summary()
    elif "--calibration" in sys.argv:
        show_calibration()
    elif "--once" in sys.argv:
        run_once()
    elif "--dry-run" in sys.argv:
        dry_run()
    else:
        main_loop()
