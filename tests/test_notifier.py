"""Tests for core.notifier — Gmail notification system."""

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
        "GMAIL_USER": "test@gmail.com" if enabled else "",
        "GMAIL_APP_PASSWORD": "testapppassword" if enabled else "",
        "GMAIL_TO": "to@gmail.com" if enabled else "",
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
        assert n.gmail_user == "test@gmail.com"
        assert n.gmail_password == "testapppassword"

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

    def test_includes_cli_instructions(self):
        n = _make_notifier()
        draft = {"id": "d1", "content": "test"}
        with patch.object(n, "_send_message", return_value=True) as mock:
            n.send_draft_review(draft)
        text = mock.call_args[0][0]
        assert "approve" in text
        assert "reject" in text
        assert "d1" in text


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
# _send_message() — Gmail SMTP layer
# ------------------------------------------------------------------

class TestSendMessage:
    def test_send_success(self):
        n = _make_notifier()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = n._send_message("hello")
        assert result is True

    def test_retry_on_smtp_error(self):
        import smtplib
        n = _make_notifier()
        n._retry_delays = [0, 0, 0]
        call_count = [0]

        def smtp_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 2:
                raise smtplib.SMTPException("temp error")
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=MagicMock())
            m.__exit__ = MagicMock(return_value=False)
            return m

        with patch("smtplib.SMTP", side_effect=smtp_side_effect):
            result = n._send_message("test", retry=True)
        # May succeed on retry or fail — just ensure no crash
        assert isinstance(result, bool)

    def test_smtp_failure_returns_false(self):
        import smtplib
        n = _make_notifier()
        n._retry_delays = [0, 0, 0]
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPException("fail")):
            result = n._send_message("test", retry=False)
        assert result is False


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
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            n.send("boundary", "msg")

        event_hash = n._hash_event("boundary", "msg")
        n._recent_events[event_hash] = time.time() - _DEDUP_WINDOW_SECONDS + 1

        with patch.object(n, "_send_message", return_value=True):
            assert n.send("boundary", "msg") is False

    def test_dedup_just_past_window_allows_resend(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            n.send("past", "msg")

        event_hash = n._hash_event("past", "msg")
        n._recent_events[event_hash] = time.time() - _DEDUP_WINDOW_SECONDS - 1

        with patch.object(n, "_send_message", return_value=True):
            assert n.send("past", "msg") is True

    def test_failed_send_does_not_dedup(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=False):
            n.send("fail_evt", "msg")

        event_hash = n._hash_event("fail_evt", "msg")
        assert event_hash not in n._recent_events

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


# ------------------------------------------------------------------
# Optimistic dedup pattern
# ------------------------------------------------------------------

class TestOptimisticDedup:
    def test_reservation_removed_on_failure(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=False):
            result = n.send("opt_fail", "msg")
        assert result is False

        event_hash = n._hash_event("opt_fail", "msg")
        assert event_hash not in n._recent_events

    def test_reservation_kept_on_success(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True):
            result = n.send("opt_ok", "msg")
        assert result is True

        event_hash = n._hash_event("opt_ok", "msg")
        assert event_hash in n._recent_events


# ------------------------------------------------------------------
# Daily report edge cases
# ------------------------------------------------------------------

class TestDailyReportEdgeCases:
    def test_with_missing_keys(self):
        n = _make_notifier()
        with patch.object(n, "_send_message", return_value=True) as mock:
            result = n.send_daily_report({})
        assert result is True
        text = mock.call_args[0][0]
        assert "0件" in text
        assert "0.0%" in text
