"""Telegram notification system for ThreadsBot.

Sends alerts and reports via Telegram Bot API.
Supports severity-based retry, deduplication (30min window),
and inline keyboard buttons for draft review.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import threading
import time
from typing import Any

import httpx
import yaml

from core.logger import get_logger

logger = get_logger("notifier")

# Severity levels
SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
SEVERITY_CRITICAL = "critical"

# Defaults (overridden by config/settings.yaml → notifications)
_DEFAULT_DEDUP_WINDOW_MINUTES = 30
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAYS = [2, 5, 15]

# Backward-compatible aliases for test imports
_DEDUP_WINDOW_SECONDS = _DEFAULT_DEDUP_WINDOW_MINUTES * 60
_MAX_RETRIES = _DEFAULT_MAX_RETRIES
_RETRY_DELAYS = _DEFAULT_RETRY_DELAYS

_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "settings.yaml",
)


def _load_notification_config() -> dict[str, Any]:
    """Load the ``notifications`` section from settings.yaml."""
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("notifications", {})
    except (OSError, yaml.YAMLError):
        return {}


class Notifier:
    """Send notifications via Telegram Bot API.

    Configuration is read from environment variables:
    - ``TELEGRAM_BOT_TOKEN``: Bot API token from @BotFather
    - ``TELEGRAM_CHAT_ID``: Target chat/group ID

    If either is missing, all send operations silently no-op.
    """

    def __init__(self) -> None:
        self.bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled: bool = bool(self.bot_token and self.chat_id)
        self._recent_events: dict[str, float] = {}  # hash → timestamp
        self._lock = threading.Lock()
        self._http: httpx.Client | None = None

        # Load config from settings.yaml
        cfg = _load_notification_config()
        dedup_mins = cfg.get("dedup_window_minutes", _DEFAULT_DEDUP_WINDOW_MINUTES)
        self._dedup_window_seconds: int = dedup_mins * 60
        self._max_retries: int = cfg.get("retry_max", _DEFAULT_MAX_RETRIES)
        self._retry_delays: list[int] = cfg.get("retry_delays", _DEFAULT_RETRY_DELAYS)

        if not self.enabled:
            logger.info(
                "Notifier disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
            )

    # ==================================================================
    # Public API
    # ==================================================================

    def send(
        self,
        event_type: str,
        message: str,
        severity: str = SEVERITY_LOW,
    ) -> bool:
        """Send a notification message.

        Args:
            event_type: Event identifier (e.g. ``"circuit_breaker"``).
            message: Human-readable message body.
            severity: One of ``"low"``, ``"medium"``, ``"high"``, ``"critical"``.

        Returns:
            ``True`` if sent successfully, ``False`` otherwise.
        """
        if not self.enabled:
            logger.debug("Notifier disabled, skipping: %s", event_type)
            return False

        # Dedup: skip if same event was sent within 30 minutes.
        # Uses optimistic recording: reserve the slot before sending so
        # concurrent threads see the reservation and skip.  If the send
        # fails, the reservation is removed.
        event_hash = self._hash_event(event_type, message)
        now = time.time()

        with self._lock:
            self._cleanup_old_events(now)
            if event_hash in self._recent_events:
                logger.debug("Dedup suppressed: %s", event_type)
                return False
            # Optimistic: reserve the slot before sending
            self._recent_events[event_hash] = now

        # Format message with severity prefix
        prefix = self._severity_prefix(severity)
        full_message = f"{prefix} {message}"

        # Send with retry for high/critical
        should_retry = severity in (SEVERITY_HIGH, SEVERITY_CRITICAL)
        success = self._send_message(full_message, retry=should_retry)

        if success:
            logger.info("Notification sent: [%s] %s", event_type, severity)
        else:
            # Remove reservation so the event can be retried next time
            with self._lock:
                self._recent_events.pop(event_hash, None)
            logger.error("Notification failed: [%s] %s", event_type, severity)

        return success

    def send_draft_review(self, draft: dict[str, Any]) -> bool:
        """Send a draft for Telegram inline review.

        Sends the draft content with inline keyboard buttons:
        [OK] [NG] [あとで]

        Args:
            draft: Draft dict with ``id``, ``content``, ``quality_score``, etc.

        Returns:
            ``True`` if sent successfully.
        """
        if not self.enabled:
            return False

        draft_id = draft.get("id", "unknown")
        score = draft.get("quality_score", "?")
        pattern = draft.get("pattern", "?")
        category = draft.get("category", "?")
        content = draft.get("content", "")

        text = (
            f"📝 レビュー待ち\n"
            f"ID: {draft_id}\n"
            f"スコア: {score} | パターン: {pattern}\n"
            f"カテゴリ: {category}\n"
            f"━━━━━━━━━━\n"
            f"{content}"
        )

        # Thread posts
        thread_posts = draft.get("thread_posts")
        if thread_posts:
            for i, tp in enumerate(thread_posts, 1):
                text += f"\n\n─── thread {i} ───\n{tp}"

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ OK", "callback_data": f"approve:{draft_id}"},
                    {"text": "❌ NG", "callback_data": f"reject:{draft_id}"},
                    {"text": "⏰ あとで", "callback_data": f"later:{draft_id}"},
                ]
            ]
        }

        return self._send_message(
            text, reply_markup=inline_keyboard
        )

    def send_daily_report(self, report: dict[str, Any]) -> bool:
        """Send a daily KPI report.

        Args:
            report: Dict with keys like ``posts_today``, ``avg_engagement``,
                ``followers_change``, etc.

        Returns:
            ``True`` if sent successfully.
        """
        if not self.enabled:
            return False

        lines = [
            "📊 Daily Report",
            f"投稿: {report.get('posts_today', 0)}件",
            f"Avg Engagement: {report.get('avg_engagement') or 0:.1f}%",
            f"フォロワー: {report.get('followers_change', '+0')}",
            f"品質スコア平均: {report.get('avg_quality_score') or 0:.1f}",
        ]

        top_post = report.get("top_post")
        if top_post:
            lines.append(f"\n🏆 Top: {top_post[:80]}")

        return self._send_message("\n".join(lines))

    # ==================================================================
    # Telegram Bot API
    # ==================================================================

    def _get_http_client(self) -> httpx.Client:
        """Return a reusable httpx.Client, creating one if needed."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.Client(timeout=10)
        return self._http

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None and not self._http.is_closed:
            self._http.close()
            self._http = None

    def _send_message(
        self,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        retry: bool = False,
    ) -> bool:
        """Send a message via Telegram Bot API.

        Args:
            text: Message text (max 4096 chars, truncated if longer).
            parse_mode: Optional ``"Markdown"`` or ``"HTML"``.
            reply_markup: Optional inline keyboard markup.
            retry: Whether to retry on failure.

        Returns:
            ``True`` if the API returned success.
        """
        if len(text) > 4096:
            text = text[:4090] + "\n..."

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        max_attempts = self._max_retries if retry else 1
        client = self._get_http_client()

        for attempt in range(max_attempts):
            try:
                resp = client.post(url, json=payload)

                if resp.status_code == 200:
                    return True

                logger.warning(
                    "Telegram API error %d: %s (attempt %d/%d)",
                    resp.status_code,
                    resp.text[:200],
                    attempt + 1,
                    max_attempts,
                )

            except httpx.HTTPError as exc:
                logger.warning(
                    "Telegram HTTP error: %s (attempt %d/%d)",
                    exc,
                    attempt + 1,
                    max_attempts,
                )
                # Recreate client on connection errors
                self.close()
                client = self._get_http_client()

            if attempt < max_attempts - 1:
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                time.sleep(delay)

        return False

    # ==================================================================
    # Internals
    # ==================================================================

    @staticmethod
    def _severity_prefix(severity: str) -> str:
        return {
            SEVERITY_LOW: "[INFO]",
            SEVERITY_MEDIUM: "[WARN]",
            SEVERITY_HIGH: "[ALERT]",
            SEVERITY_CRITICAL: "[CRITICAL]",
        }.get(severity, "[INFO]")

    @staticmethod
    def _hash_event(event_type: str, message: str) -> str:
        raw = f"{event_type}:{message}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def _cleanup_old_events(self, now: float) -> None:
        cutoff = now - self._dedup_window_seconds
        expired = [k for k, ts in self._recent_events.items() if ts < cutoff]
        for k in expired:
            del self._recent_events[k]
