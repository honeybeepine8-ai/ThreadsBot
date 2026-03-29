"""Telegram Bot — remote status/kill commands for ThreadsBot.

Usage::

    python scripts/telegram_bot.py   # start long-polling

Commands:
    /status  — show current dashboard status
    /stop <reason>  — trigger emergency stop
    /clear  — clear emergency stop

Designed to run as a background thread alongside the scheduler daemon.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import httpx

from core.logger import get_logger
from core.state_manager import StateManager
from core.safety import SafetyGuard
from scripts.dashboard import render_dashboard

logger = get_logger("telegram_bot")

_POLL_TIMEOUT = 30  # seconds for long polling


class TelegramCommandBot:
    """Handle Telegram commands via long polling.

    Supports ``/status``, ``/stop <reason>``, and ``/clear``.
    """

    def __init__(self) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.sm = StateManager()
        self.safety = SafetyGuard(self.sm)
        self._offset: int | None = None
        self._http = httpx.Client(timeout=_POLL_TIMEOUT + 10)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """Poll Telegram for updates and dispatch commands."""
        if not self.enabled:
            logger.warning("Telegram bot disabled: missing BOT_TOKEN or CHAT_ID.")
            return

        logger.info("Telegram command bot started.")
        try:
            while True:
                try:
                    updates = self._get_updates()
                    for update in updates:
                        self._handle_update(update)
                except KeyboardInterrupt:
                    logger.info("Telegram bot stopped.")
                    break
                except Exception as exc:
                    logger.error("Telegram poll error: %s", exc)
                    time.sleep(5)
        finally:
            self._http.close()

    # ------------------------------------------------------------------
    # Telegram API
    # ------------------------------------------------------------------

    def _get_updates(self) -> list[dict]:
        """Long-poll for new messages."""
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params: dict = {"timeout": _POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            resp = self._http.get(url, params=params)
            if resp.status_code != 200:
                logger.warning("getUpdates failed: %d", resp.status_code)
                return []

            data = resp.json()
            if not isinstance(data, dict):
                logger.warning("getUpdates returned unexpected type: %s", type(data))
                return []
            results: list[dict] = data.get("result", [])
            if results:
                last_id = results[-1].get("update_id")
                if last_id is not None:
                    self._offset = last_id + 1
                else:
                    logger.warning("Last update missing update_id — offset unchanged")
            return results

        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("getUpdates error: %s", exc)
            return []

    def _send_reply(self, chat_id: int | str, text: str) -> None:
        """Send a text reply."""
        if len(text) > 4096:
            text = text[:4090] + "\n..."

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}

        try:
            self._http.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.error("sendMessage failed: %s", exc)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _handle_update(self, update: dict) -> None:
        """Parse and dispatch a single update."""
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")

        if not text or not chat_id:
            return

        # Security: only accept commands from configured chat
        if str(chat_id) != str(self.chat_id):
            logger.warning("Ignoring command from unauthorized chat: %s", chat_id)
            return

        if text == "/status":
            self._cmd_status(chat_id)
        elif text == "/stop" or text.startswith("/stop "):
            reason = text[5:].strip() or "Telegram remote stop"
            self._cmd_stop(chat_id, reason)
        elif text == "/clear":
            self._cmd_clear(chat_id)
        else:
            self._send_reply(
                chat_id,
                "Unknown command. Available:\n/status\n/stop <reason>\n/clear",
            )

    def _cmd_status(self, chat_id: int | str) -> None:
        """Handle /status command."""
        try:
            status_text = render_dashboard(self.sm)
            self._send_reply(chat_id, status_text)
        except Exception as exc:
            self._send_reply(chat_id, f"Status error: {exc}")

    def _cmd_stop(self, chat_id: int | str, reason: str) -> None:
        """Handle /stop command."""
        try:
            self.safety.emergency_stop(reason)
            self._send_reply(
                chat_id,
                f"Emergency stop activated.\nReason: {reason}",
            )
            logger.info("Emergency stop via Telegram: %s", reason)
        except Exception as exc:
            self._send_reply(chat_id, f"Stop failed: {exc}")

    def _cmd_clear(self, chat_id: int | str) -> None:
        """Handle /clear command."""
        try:
            self.safety.clear_emergency_stop()
            self._send_reply(chat_id, "Emergency stop cleared.")
            logger.info("Emergency stop cleared via Telegram.")
        except Exception as exc:
            self._send_reply(chat_id, f"Clear failed: {exc}")


def main() -> None:
    bot = TelegramCommandBot()
    bot.run_forever()


if __name__ == "__main__":
    main()
