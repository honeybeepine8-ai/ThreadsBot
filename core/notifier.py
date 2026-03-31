"""Gmail notification system for ThreadsBot.

Sends alerts and reports via Gmail SMTP (App Password).
Supports severity-based retry, deduplication (30min window),
and draft review emails with CLI approval instructions.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import smtplib
import threading
import time
from email.mime.text import MIMEText
from typing import Any

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
    """Send notifications via Gmail SMTP.

    Configuration is read from environment variables:
    - ``GMAIL_USER``: Gmail address used as sender
    - ``GMAIL_APP_PASSWORD``: Google App Password (16-char)
    - ``GMAIL_TO``: Recipient address (defaults to GMAIL_USER)

    If GMAIL_USER or GMAIL_APP_PASSWORD is missing, all send operations
    silently no-op.
    """

    def __init__(self) -> None:
        self.gmail_user: str = os.environ.get("GMAIL_USER", "")
        self.gmail_password: str = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.gmail_to: str = os.environ.get("GMAIL_TO", self.gmail_user)
        self.enabled: bool = bool(self.gmail_user and self.gmail_password)
        self._recent_events: dict[str, float] = {}  # hash → timestamp
        self._lock = threading.Lock()

        # Load config from settings.yaml
        cfg = _load_notification_config()
        dedup_mins = cfg.get("dedup_window_minutes", _DEFAULT_DEDUP_WINDOW_MINUTES)
        self._dedup_window_seconds: int = dedup_mins * 60
        self._max_retries: int = cfg.get("retry_max", _DEFAULT_MAX_RETRIES)
        self._retry_delays: list[int] = cfg.get("retry_delays", _DEFAULT_RETRY_DELAYS)

        if not self.enabled:
            logger.info(
                "Notifier disabled: GMAIL_USER or GMAIL_APP_PASSWORD not set."
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

        # Dedup: skip if same event was sent within the dedup window.
        event_hash = self._hash_event(event_type, message)
        now = time.time()

        with self._lock:
            self._cleanup_old_events(now)
            if event_hash in self._recent_events:
                logger.debug("Dedup suppressed: %s", event_type)
                return False
            # Optimistic: reserve the slot before sending
            self._recent_events[event_hash] = now

        prefix = self._severity_prefix(severity)
        full_message = f"{prefix} {message}"
        subject = f"[ThreadsBot][{severity.upper()}] {event_type}"

        should_retry = severity in (SEVERITY_HIGH, SEVERITY_CRITICAL)
        success = self._send_message(full_message, subject=subject, retry=should_retry)

        if success:
            logger.info("Notification sent: [%s] %s", event_type, severity)
        else:
            with self._lock:
                self._recent_events.pop(event_hash, None)
            logger.error("Notification failed: [%s] %s", event_type, severity)

        return success

    def send_draft_review(self, draft: dict[str, Any]) -> bool:
        """Send a draft review request via email.

        Includes draft content and CLI instructions for approval/rejection.

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

        lines = [
            "レビュー待ち下書き",
            f"ID: {draft_id}",
            f"スコア: {score} | パターン: {pattern}",
            f"カテゴリ: {category}",
            "━" * 20,
            content,
        ]

        thread_posts = draft.get("thread_posts")
        if thread_posts:
            for i, tp in enumerate(thread_posts, 1):
                lines.append(f"\n─── thread {i} ───\n{tp}")

        lines += [
            "",
            "━" * 20,
            "承認: python scripts/review.py approve " + draft_id,
            "却下: python scripts/review.py reject " + draft_id,
        ]

        text = "\n".join(lines)
        subject = f"[ThreadsBot] レビュー待ち: {draft_id} (スコア {score})"
        return self._send_message(text, subject=subject)

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
            "Daily Report",
            f"投稿: {report.get('posts_today', 0)}件",
            f"Avg Engagement: {report.get('avg_engagement') or 0:.1f}%",
            f"フォロワー: {report.get('followers_change', '+0')}",
            f"品質スコア平均: {report.get('avg_quality_score') or 0:.1f}",
        ]

        top_post = report.get("top_post")
        if top_post:
            lines.append(f"\nTop: {top_post[:80]}")

        subject = f"[ThreadsBot] Daily Report {datetime.date.today()}"
        return self._send_message("\n".join(lines), subject=subject)

    def close(self) -> None:
        """No-op: kept for API compatibility."""

    # ==================================================================
    # Gmail SMTP
    # ==================================================================

    def _send_message(
        self,
        text: str,
        *,
        subject: str = "ThreadsBot通知",
        retry: bool = False,
    ) -> bool:
        """Send an email via Gmail SMTP.

        Args:
            text: Email body text.
            subject: Email subject line.
            retry: Whether to retry on failure.

        Returns:
            ``True`` if sent successfully.
        """
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.gmail_user
        msg["To"] = self.gmail_to

        max_attempts = self._max_retries if retry else 1

        for attempt in range(max_attempts):
            try:
                with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
                    server.ehlo()
                    server.starttls()
                    server.login(self.gmail_user, self.gmail_password)
                    server.send_message(msg)
                return True

            except smtplib.SMTPException as exc:
                logger.warning(
                    "Gmail SMTP error: %s (attempt %d/%d)",
                    exc,
                    attempt + 1,
                    max_attempts,
                )
            except OSError as exc:
                logger.warning(
                    "Gmail connection error: %s (attempt %d/%d)",
                    exc,
                    attempt + 1,
                    max_attempts,
                )

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
