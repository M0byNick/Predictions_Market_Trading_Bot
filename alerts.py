"""
Telegram notification system for mobile alerts and trade approval.

This module sends structured alerts to your Telegram chat when the screeners
find a potential trade. If REQUIRE_APPROVAL is True (recommended), it waits
for you to reply "yes" or "no" before executing.

Setup:
  1. Message @BotFather on Telegram → /newbot → name it
  2. Copy the bot token into config.py
  3. Message your bot once, then call get_chat_id() to find your chat ID
"""
import time
import requests
import config


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
            print(f"Telegram send failed: {e}")
            return False

    def send_trade_alert(self, ticker: str, category: str, side: str,
                         sizing: dict, market_title: str = "",
                         rationale: str = "") -> bool:
        """
        Send a formatted trade alert with all relevant details.
        Designed for quick mobile review — all key info at a glance.
        """
        emoji = {"crypto": "₿", "weather": "🌡", "economics": "📈"}.get(category, "📊")

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
            msg += "\n<b>Reply YES to execute, NO to skip.</b>"

        return self.send(msg)

    def send_execution_confirmation(self, ticker: str, num_contracts: int,
                                    cost_usd: float):
        """Notify that a trade was executed."""
        self.send(
            f"✅ <b>Executed:</b> {num_contracts} contracts on "
            f"<code>{ticker}</code> for ${cost_usd:.2f}"
        )

    def send_settlement(self, ticker: str, outcome: str, pnl: float):
        """Notify when a contract settles."""
        emoji = "🟢" if pnl >= 0 else "🔴"
        self.send(
            f"{emoji} <b>Settled:</b> <code>{ticker}</code>\n"
            f"Outcome: {outcome.upper()} | P&L: ${pnl:+.2f}"
        )

    def send_daily_summary(self, summary: str):
        """Send the daily performance summary."""
        self.send(f"📋 <b>Daily Summary</b>\n\n<pre>{summary}</pre>")

    def wait_for_approval(self, timeout_seconds: int = 300) -> bool | None:
        """
        Poll for a YES/NO reply. Returns True if approved, False if rejected,
        None if timeout. Checks every 5 seconds for up to `timeout_seconds`.

        This is the core of the mobile-friendly workflow: you get an alert,
        glance at it on your phone, and reply YES or NO.
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
                    msg = update.get("message", {})

                    # Only accept messages from our configured chat
                    if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                        continue

                    text = msg.get("text", "").strip().lower()
                    if text in ("yes", "y", "approve", "go"):
                        return True
                    elif text in ("no", "n", "reject", "skip", "pass"):
                        return False

            except Exception as e:
                print(f"Telegram poll error: {e}")
                time.sleep(5)

        return None  # Timed out

    def get_chat_id(self):
        """
        Helper to discover your chat ID. Send any message to your bot
        first, then call this method to print the chat ID.
        """
        resp = requests.get(f"{self.base_url}/getUpdates", timeout=10)
        updates = resp.json().get("result", [])
        for update in updates:
            chat = update.get("message", {}).get("chat", {})
            print(f"Chat ID: {chat.get('id')} | Name: {chat.get('first_name', '')}")
