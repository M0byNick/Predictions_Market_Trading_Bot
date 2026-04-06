"""
Telegram notification system for mobile alerts and trade approval.

This module sends structured alerts to your Telegram chat when the screeners
find a potential trade. If REQUIRE_APPROVAL is True (recommended), it waits
for you to reply "yes" or "no" before executing.

Enhanced in Tier 3 with:
  - Inline keyboard buttons for one-tap approve/reject
  - Expiration alerts (24h and 1h before settlement)
  - Daily digest with open positions, P&L, and calibration drift
  - Weekly calibration summary

Setup:
  1. Message @BotFather on Telegram -> /newbot -> name it
  2. Copy the bot token into config.py
  3. Message your bot once, then call get_chat_id() to find your chat ID
"""
from __future__ import annotations

import json
import time
import requests
import config
from log import logger


class TelegramAlerter:
    """Sends trade alerts and handles approval flow via Telegram."""

    def __init__(self):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id = 0

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured Telegram chat."""
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                },
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False

    def send_with_buttons(self, message: str, buttons: list,
                          parse_mode: str = "HTML") -> dict:
        """
        Send a message with inline keyboard buttons for one-tap actions.

        Args:
            message: HTML-formatted message text.
            buttons: List of [{"text": "label", "callback_data": "value"}, ...] rows.
                     Each row is a list of button dicts.
        Returns:
            The sent message dict (for tracking message_id).
        """
        keyboard = {"inline_keyboard": buttons}
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                    "reply_markup": json.dumps(keyboard),
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("result", {})
            return {}
        except Exception as e:
            logger.error("Telegram send_with_buttons failed: %s", e)
            return {}

    def send_trade_alert(self, ticker: str, category: str, side: str,
                         sizing: dict, market_title: str = "",
                         rationale: str = "") -> bool:
        """
        Send a formatted trade alert with all relevant details.
        Uses inline keyboard buttons for quick approve/reject on mobile.
        """
        emoji = {"crypto": "\u20bf", "weather": "\U0001f321", "economics": "\U0001f4c8"}.get(category, "\U0001f4ca")

        msg = (
            f"{emoji} <b>Trade Signal: {category.upper()}</b>\n"
            f"{'─' * 30}\n"
            f"<b>Market:</b> {market_title or ticker}\n"
            f"<b>Ticker:</b> <code>{ticker}</code>\n"
            f"<b>Side:</b> {side.upper()}\n"
            f"{'─' * 30}\n"
            f"<b>Your prob:</b> {sizing['your_prob']:.1%}\n"
            f"<b>Market prob:</b> {sizing['market_prob']:.1%}\n"
            f"<b>Edge:</b> {sizing['edge']:.1%}\n"
            f"{'─' * 30}\n"
            f"<b>Contracts:</b> {sizing['recommended_contracts']}\n"
            f"<b>Cost:</b> ${sizing['recommended_usd']}\n"
            f"<b>Expected value:</b> ${sizing['expected_value']}\n"
        )
        if rationale:
            msg += f"\n<b>Rationale:</b> {rationale}\n"

        if config.REQUIRE_APPROVAL:
            # Send with inline buttons for one-tap approve/reject
            buttons = [[
                {"text": "\u2705 APPROVE", "callback_data": "approve"},
                {"text": "\u274c REJECT", "callback_data": "reject"},
            ]]
            self.send_with_buttons(msg, buttons)
            return True

        return self.send(msg)

    def send_execution_confirmation(self, ticker: str, num_contracts: int,
                                    cost_usd: float):
        """Notify that a trade was executed."""
        self.send(
            f"\u2705 <b>Executed:</b> {num_contracts} contracts on "
            f"<code>{ticker}</code> for ${cost_usd:.2f}"
        )

    def send_settlement(self, ticker: str, outcome: str, pnl: float):
        """Notify when a contract settles."""
        emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
        self.send(
            f"{emoji} <b>Settled:</b> <code>{ticker}</code>\n"
            f"Outcome: {outcome.upper()} | P&L: ${pnl:+.2f}"
        )

    def send_daily_summary(self, summary: str):
        """Send the daily performance summary."""
        self.send(f"\U0001f4cb <b>Daily Summary</b>\n\n<pre>{summary}</pre>")

    def send_daily_digest(self, tracker, calibration_analyzer=None):
        """
        Send a comprehensive daily digest with:
          - Open positions and upcoming expirations
          - Yesterday's P&L
          - Calibration drift warnings
        """
        from datetime import datetime, timezone

        lines = ["\U0001f4ca <b>Daily Digest</b>"]

        # P&L summary
        lines.append(f"\n<b>Overall P&L:</b> ${tracker.total_pnl():.2f}")
        lines.append(f"<b>Hit rate:</b> {tracker.hit_rate():.1%}")

        # Per-category
        for cat in ["crypto", "weather", "economics"]:
            pnl = tracker.total_pnl(cat)
            settled = tracker.settled_trades(cat)
            if settled:
                lines.append(f"  {cat}: ${pnl:.2f} ({len(settled)} trades)")

        # Open positions count
        all_trades = tracker.trades
        open_trades = [t for t in all_trades if t["outcome"] is None]
        if open_trades:
            lines.append(f"\n<b>Open positions:</b> {len(open_trades)}")
            for t in open_trades[:5]:
                lines.append(f"  <code>{t['ticker']}</code> {t['side'].upper()}")
            if len(open_trades) > 5:
                lines.append(f"  ... and {len(open_trades) - 5} more")

        # Calibration drift warning
        if calibration_analyzer:
            bias = calibration_analyzer.confidence_bias()
            if abs(bias["bias"]) > 0.05:
                lines.append(
                    f"\n\u26a0\ufe0f <b>Calibration drift:</b> {bias['bias']:+.3f} "
                    f"({bias['direction']})"
                )

        self.send("\n".join(lines))

    def send_expiration_alert(self, ticker: str, hours_until: float,
                              side: str, cost_usd: float):
        """Alert before a contract is about to settle."""
        emoji = "\u23f0" if hours_until <= 1 else "\U0001f4e2"
        self.send(
            f"{emoji} <b>Expiring {'soon' if hours_until <= 1 else 'tomorrow'}:</b> "
            f"<code>{ticker}</code>\n"
            f"Side: {side.upper()} | Cost: ${cost_usd:.2f}\n"
            f"Settles in {hours_until:.0f}h"
        )

    def send_health_check(self, cycle_count: int, uptime_hours: float,
                          last_opp_time: str, balance_usd: float) -> bool:
        """Send a periodic health check heartbeat."""
        return self.send(
            f"\U0001f493 <b>Health Check</b>\n"
            f"Cycles: {cycle_count}\n"
            f"Uptime: {uptime_hours:.1f}h\n"
            f"Last opportunity: {last_opp_time}\n"
            f"Balance: ${balance_usd:.2f}"
        )

    def send_no_opportunity_alert(self, hours: float) -> bool:
        """Alert when no opportunities have been found for too long."""
        return self.send(
            f"\u26a0\ufe0f <b>No opportunities found in {hours:.0f}h.</b>\n"
            f"Markets may be efficiently priced, or screeners may need attention."
        )

    def send_calibration_digest(self, calibration_analyzer):
        """Send weekly calibration summary via Telegram."""
        digest = calibration_analyzer.telegram_digest()
        self.send(digest)

    def wait_for_approval(self, timeout_seconds: int = 300) -> bool | None:
        """
        Poll for a YES/NO reply or inline button callback.
        Returns True if approved, False if rejected, None if timeout.

        Checks both text messages and inline keyboard callbacks.
        """
        start = time.time()
        while time.time() - start < timeout_seconds:
            try:
                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": self._last_update_id + 1, "timeout": 5},
                    timeout=15,
                )
                updates = resp.json().get("result", [])

                for update in updates:
                    self._last_update_id = update["update_id"]

                    # Check inline keyboard callbacks
                    callback = update.get("callback_query")
                    if callback:
                        cb_chat_id = str(callback.get("message", {}).get("chat", {}).get("id"))
                        if cb_chat_id == str(self.chat_id):
                            data = callback.get("data", "")
                            # Answer the callback to dismiss the loading indicator
                            self._answer_callback(callback.get("id"))
                            if data == "approve":
                                return True
                            elif data == "reject":
                                return False

                    # Check text messages (backward compat)
                    msg = update.get("message", {})
                    if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                        continue

                    text = msg.get("text", "").strip().lower()
                    if text in ("yes", "y", "approve", "go"):
                        return True
                    elif text in ("no", "n", "reject", "skip", "pass"):
                        return False

            except Exception as e:
                logger.error("Telegram poll error: %s", e)
                time.sleep(5)

        return None  # Timed out

    def _answer_callback(self, callback_id: str) -> None:
        """Answer a callback query to dismiss the loading indicator."""
        try:
            requests.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
                timeout=5,
            )
        except Exception:
            pass

    def check_commands(self, tracker=None, client=None) -> None:
        """
        Poll for incoming Telegram messages and respond to commands.

        Supported commands:
          /status  — Current bot status, P&L, and dashboard link
          /dashboard — Dashboard link
          /balance — Paper trading balance
          /help — List available commands
        """
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self._last_update_id + 1, "timeout": 1},
                timeout=10,
            )
            updates = resp.json().get("result", [])
        except Exception as e:
            logger.error("Telegram command poll error: %s", e)
            return

        dashboard_url = "http://localhost:8501"

        for update in updates:
            self._last_update_id = update["update_id"]

            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                continue

            text = (msg.get("text") or "").strip().lower()

            if text == "/status":
                # Phase 5+ only — DB is sole P&L source of truth
                CUTOFF = "2026-03-26T04:39:00"
                lines = ["📊 <b>Bot Status (Phase 5+)</b>"]
                if tracker:
                    settled = tracker.conn.execute(
                        f"SELECT COUNT(*) FROM trades WHERE outcome IS NOT NULL AND entry_time >= '{CUTOFF}'"
                    ).fetchone()[0]
                    open_ct = tracker.conn.execute(
                        f"SELECT COUNT(*) FROM trades WHERE outcome IS NULL AND entry_time >= '{CUTOFF}'"
                    ).fetchone()[0]
                    wins = tracker.conn.execute(
                        f"SELECT COUNT(*) FROM trades WHERE outcome = 'win' AND entry_time >= '{CUTOFF}'"
                    ).fetchone()[0]
                    pnl = tracker.conn.execute(
                        f"SELECT COALESCE(SUM(pnl_usd), 0) FROM trades WHERE outcome IS NOT NULL AND entry_time >= '{CUTOFF}'"
                    ).fetchone()[0]
                    hit = wins / settled if settled else 0
                    lines.append(f"P&L: <b>${pnl:+,.2f}</b>")
                    lines.append(f"Hit rate: {hit:.1%} ({wins}W / {settled - wins}L)")
                    lines.append(f"Trades: {settled + open_ct} ({settled} settled, {open_ct} open)")
                lines.append(f"\n🖥 <a href=\"{dashboard_url}\">Dashboard</a>")
                self.send("\n".join(lines))

            elif text == "/dashboard":
                self.send(f"🖥 <b>Dashboard:</b> <a href=\"{dashboard_url}\">{dashboard_url}</a>")

            elif text == "/balance":
                CUTOFF = "2026-03-26T04:39:00"
                if tracker:
                    pnl = tracker.conn.execute(
                        f"SELECT COALESCE(SUM(pnl_usd), 0) FROM trades WHERE outcome IS NOT NULL AND entry_time >= '{CUTOFF}'"
                    ).fetchone()[0]
                    open_cost = tracker.conn.execute(
                        f"SELECT COALESCE(SUM(cost_usd), 0) FROM trades WHERE outcome IS NULL AND entry_time >= '{CUTOFF}'"
                    ).fetchone()[0]
                    self.send(
                        f"💰 <b>Phase 5+ Balance</b>\n"
                        f"Bankroll: $1,000,000\n"
                        f"Realized P&L: ${pnl:+,.2f}\n"
                        f"Open exposure: ${open_cost:,.2f}\n"
                        f"Net: ${1_000_000 + pnl - open_cost:,.2f}"
                    )
                else:
                    self.send("⚠️ No tracker available.")

            elif text == "/help":
                self.send(
                    "🤖 <b>Available commands:</b>\n"
                    "/status — Bot status, P&L, dashboard link\n"
                    "/dashboard — Dashboard link\n"
                    "/balance — Paper trading balance\n"
                    "/help — This message"
                )

    def get_chat_id(self):
        """
        Helper to discover your chat ID. Send any message to your bot
        first, then call this method to print the chat ID.
        """
        resp = requests.get(f"{self.base_url}/getUpdates", timeout=10)
        updates = resp.json().get("result", [])
        for update in updates:
            chat = update.get("message", {}).get("chat", {})
            logger.info("Chat ID: %s | Name: %s", chat.get('id'), chat.get('first_name', ''))
