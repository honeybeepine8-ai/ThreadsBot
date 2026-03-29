"""Tests for core.notifier — Telegram notification system."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from core.notifier import (
    Notifier,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_HIGH,
    SEVERITY_CRITICAL,
    _DEDUP_WINDOW_SECONDS,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_notifier(*, enabled: bool = True) -> Notifier:
    """Create a Notifier with mocked env vars."""
    env = {
        "TELEGRAM_BOT_TOKEN": "test-token" if enabled else "",
        "TELEGRAM_CHAT_ID": "12345" if enabled else "",
    }
    with patch.dict("os.environ", env, clear=False):
        return Notifier()


# ------------------------------------------------------------------
# Initialization
# ------------------------------------------------------------------

class TestNotifierInit:
    def test_enabled_when_both_vars_set(self):
        n = _make_notifier(enabled=True)
        assert n.enabled is True
        assert n.bot_token == "test-token"
        assert n.chat_id == "12345"

    def test_disabled_when_vars_missing(self):
        n = _make_notifier(enabled=False)
        assert n.enabled is False


# ------------------------------------------------------------------
# send()
# ------------------------------------------------------------------

class TestSend:
    def test_send_disabled_returns_false(self):
        n = _make_notifier(enabled=False)
        assert n.send("test", "hello") is False

    def test_send_success(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True) as mock:
            result = n.send("test_event", "Test message", SEVERITY_LOW)
        assert result is True
        mock.assert_called_once()
        call_text = mock.call_args[0][0]
        assert "[INFO]" in call_text
        assert "Test message" in call_text

    def test_send_failure(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=False):
            result = n.send("test_event", "fail", SEVERITY_LOW)
        assert result is False

    def test_severity_prefixes(self):
        n = _make_notifier()
        for sev, prefix in [
            (SEVERITY_LOW, "[INFO]"),
            (SEVERITY_MEDIUM, "[WARN]"),
            (SEVERITY_HIGH, "[ALERT]"),
            (SEVERITY_CRITICAL, "[CRITICAL]"),
        ]:
            with patch.object(n, "_send_message", return_value=True) as mock:
                n._recent_events.clear()
                n.send("evt", "msg", sev)
            assert prefix in mock.call_args[0][0]


# ------------------------------------------------------------------
# Deduplication
# ------------------------------------------------------------------

class TestDedup:
    def test_duplicate_suppressed_within_window(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            assert n.send("dup_event", "same msg") is True
            assert n.send("dup_event", "same msg") is False

    def test_different_events_not_suppressed(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            assert n.send("event_a", "msg") is True
            assert n.send("event_b", "msg") is True

    def test_duplicate_allowed_after_window(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            n.send("old_event", "msg")

        # Simulate time passing beyond dedup window
        event_hash = n._hash_event("old_event", "msg")
        n._recent_events[event_hash] = time.time() - _DEDUP_WINDOW_SECONDS - 1

        with patch.object(n, "_send_message", return_value=True):
            assert n.send("old_event", "msg") is True


# ------------------------------------------------------------------
# Retry logic
# ------------------------------------------------------------------

class TestRetry:
    def test_high_severity_retries(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True) as mock:
            n.send("alert", "critical issue", SEVERITY_HIGH)
        mock.assert_called_once()
        assert mock.call_args[1].get("retry") is True

    def test_low_severity_no_retry(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True) as mock:
            n.send("info", "just info", SEVERITY_LOW)
        assert mock.call_args[1].get("retry") is False


# ------------------------------------------------------------------
# send_draft_review()
# ------------------------------------------------------------------

class TestSendDraftReview:
    def test_disabled_returns_false(self):
        n = _make_notifier(enabled=False)
        assert n.send_draft_review({"id": "d1", "content": "test"}) is False

    def test_formats_correctly(self):
        n = _make_notifier()
        draft = {
            "id": "draft_001",
            "content": "skincare tip",
            "quality_score": 8.5,
            "pattern": "tips",
            "category": "skincare_knowledge",
        }
        with patch.object(n, "_send_message", return_value=True) as mock:
            result = n.send_draft_review(draft)
        assert result is True
        text = mock.call_args[0][0]
        assert "draft_001" in text
        assert "8.5" in text
        assert "skincare tip" in text

    def test_includes_inline_keyboard(self):
        n = _make_notifier()
        draft = {"id": "d1", "content": "test"}
        with patch.object(n, "_send_message", return_value=True) as mock:
            n.send_draft_review(draft)
        reply_markup = mock.call_args[1].get("reply_markup")
        assert reply_markup is not None
        buttons = reply_markup["inline_keyboard"][0]
        assert len(buttons) == 3
        assert "approve:d1" in buttons[0]["callback_data"]


# ------------------------------------------------------------------
# send_daily_report()
# ------------------------------------------------------------------

class TestSendDailyReport:
    def test_disabled_returns_false(self):
        n = _make_notifier(enabled=False)
        assert n.send_daily_report({}) is False

    def test_formats_kpi(self):
        n = _make_notifier()
        report = {
            "posts_today": 6,
            "avg_engagement": 4.2,
            "followers_change": "+15",
            "avg_quality_score": 8.1,
            "top_post": "Best skincare tip ever",
        }
        with patch.object(n, "_send_message", return_value=True) as mock:
            result = n.send_daily_report(report)
        assert result is True
        text = mock.call_args[0][0]
        assert "6" in text
        assert "4.2" in text


# ------------------------------------------------------------------
# _send_message() — Telegram API layer
# ------------------------------------------------------------------

class TestSendMessage:
    def test_truncates_long_text(self):
        n = _make_notifier()
        long_text = "x" * 5000
        mock_resp = MagicMock(status_code=200)
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        with patch.object(n, "_get_http_client", return_value=mock_client):
            result = n._send_message(long_text)
        assert result is True
        # Verify the sent text was truncated
        sent_text = mock_client.post.call_args[1]["json"]["text"]
        assert len(sent_text) <= 4096

    def test_retry_on_failure(self):
        n = _make_notifier()
        fail_resp = MagicMock(status_code=500, text="error")
        ok_resp = MagicMock(status_code=200)

        mock_client = MagicMock()
        mock_client.post = MagicMock(side_effect=[fail_resp, ok_resp])
        mock_client.is_closed = False

        n._retry_delays = [0, 0, 0]
        with patch.object(n, "_get_http_client", return_value=mock_client):
            result = n._send_message("test", retry=True)
        assert result is True
        assert mock_client.post.call_count == 2


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

class TestEdgeCases:
    def test_hash_event_deterministic(self):
        h1 = Notifier._hash_event("type_a", "msg")
        h2 = Notifier._hash_event("type_a", "msg")
        assert h1 == h2

    def test_hash_event_different_for_different_input(self):
        h1 = Notifier._hash_event("type_a", "msg")
        h2 = Notifier._hash_event("type_b", "msg")
        assert h1 != h2

    def test_cleanup_old_events(self):
        n = _make_notifier()
        old_time = time.time() - _DEDUP_WINDOW_SECONDS - 100
        n._recent_events["old_hash"] = old_time
        n._recent_events["new_hash"] = time.time()
        n._cleanup_old_events(time.time())
        assert "old_hash" not in n._recent_events
        assert "new_hash" in n._recent_events


# ------------------------------------------------------------------
# Dedup boundary conditions
# ------------------------------------------------------------------

class TestDedupBoundary:
    def test_dedup_at_exact_window_boundary_still_suppressed(self):
        """Event sent exactly at the dedup window edge should still be suppressed."""
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            n.send("boundary", "msg")

        # Set the event timestamp to exactly the edge of the window
        event_hash = n._hash_event("boundary", "msg")
        n._recent_events[event_hash] = time.time() - _DEDUP_WINDOW_SECONDS + 1

        with patch.object(n, "_send_message", return_value=True):
            # Still within window → should be suppressed
            assert n.send("boundary", "msg") is False

    def test_dedup_just_past_window_allows_resend(self):
        """Event sent just past the dedup window should be allowed."""
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            n.send("past", "msg")

        event_hash = n._hash_event("past", "msg")
        n._recent_events[event_hash] = time.time() - _DEDUP_WINDOW_SECONDS - 1

        with patch.object(n, "_send_message", return_value=True):
            assert n.send("past", "msg") is True

    def test_failed_send_does_not_dedup(self):
        """If a send fails, the event should NOT be marked as sent."""
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=False):
            n.send("fail_evt", "msg")

        # Event should not be in recent events since send failed
        event_hash = n._hash_event("fail_evt", "msg")
        assert event_hash not in n._recent_events

        # Retry should go through
        with patch.object(n, "_send_message", return_value=True):
            assert n.send("fail_evt", "msg") is True


# ------------------------------------------------------------------
# Retry detailed tests
# ------------------------------------------------------------------

class TestRetryDetailed:
    def test_critical_severity_retries(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True) as mock:
            n.send("crit", "urgent", SEVERITY_CRITICAL)
        assert mock.call_args[1].get("retry") is True

    def test_medium_severity_no_retry(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True) as mock:
            n.send("warn", "warning", SEVERITY_MEDIUM)
        assert mock.call_args[1].get("retry") is False

    def test_max_retry_attempts_exhausted(self):
        """After 3 failed attempts, _send_message returns False."""
        n = _make_notifier()
        fail_resp = MagicMock(status_code=500, text="error")
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=fail_resp)
        mock_client.is_closed = False

        n._retry_delays = [0, 0, 0]
        with patch.object(n, "_get_http_client", return_value=mock_client):
            result = n._send_message("test", retry=True)
        assert result is False
        assert mock_client.post.call_count == 3  # _max_retries

    def test_no_retry_single_attempt(self):
        """Without retry flag, only 1 attempt is made."""
        n = _make_notifier()
        fail_resp = MagicMock(status_code=500, text="error")
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=fail_resp)
        mock_client.is_closed = False

        with patch.object(n, "_get_http_client", return_value=mock_client):
            result = n._send_message("test", retry=False)
        assert result is False
        assert mock_client.post.call_count == 1


# ------------------------------------------------------------------
# Truncation tests
# ------------------------------------------------------------------

class TestTruncation:
    def test_exact_4096_not_truncated(self):
        """Message at exactly 4096 chars should NOT be truncated."""
        n = _make_notifier()
        text = "x" * 4096
        mock_resp = MagicMock(status_code=200)
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        with patch.object(n, "_get_http_client", return_value=mock_client):
            n._send_message(text)
        sent_text = mock_client.post.call_args[1]["json"]["text"]
        assert len(sent_text) == 4096
        assert "..." not in sent_text

    def test_4097_gets_truncated(self):
        """Message at 4097 chars should be truncated to 4094 (4090 + newline + ...)."""
        n = _make_notifier()
        text = "x" * 4097
        mock_resp = MagicMock(status_code=200)
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        with patch.object(n, "_get_http_client", return_value=mock_client):
            n._send_message(text)
        sent_text = mock_client.post.call_args[1]["json"]["text"]
        assert len(sent_text) <= 4096
        assert sent_text.endswith("...")

    def test_daily_report_with_missing_keys(self):
        """send_daily_report should handle missing keys gracefully."""
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True) as mock:
            result = n.send_daily_report({})
        assert result is True
        text = mock.call_args[0][0]
        assert "0件" in text
        assert "0.0%" in text

    def test_send_prefix_plus_long_message_truncates(self):
        """send() adds prefix before _send_message — verify combined truncation."""
        n = _make_notifier()
        long_msg = "x" * 4090
        mock_resp = MagicMock(status_code=200)
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        with patch.object(n, "_get_http_client", return_value=mock_client):
            n.send("long_test", long_msg, SEVERITY_CRITICAL)
        sent_text = mock_client.post.call_args[1]["json"]["text"]
        assert len(sent_text) <= 4096
        assert sent_text.startswith("[CRITICAL]")


# ------------------------------------------------------------------
# Optimistic dedup pattern
# ------------------------------------------------------------------

class TestOptimisticDedup:
    def test_reservation_removed_on_failure(self):
        """If send fails, the dedup reservation should be cleared for retry."""
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=False):
            result = n.send("opt_fail", "msg")
        assert result is False

        # Reservation should be cleared
        event_hash = n._hash_event("opt_fail", "msg")
        assert event_hash not in n._recent_events

    def test_reservation_kept_on_success(self):
        """If send succeeds, the dedup reservation should persist."""
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            result = n.send("opt_ok", "msg")
        assert result is True

        event_hash = n._hash_event("opt_ok", "msg")
        assert event_hash in n._recent_events
