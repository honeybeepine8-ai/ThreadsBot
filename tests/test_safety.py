"""Tests for core.safety.SafetyGuard and ComplianceChecker."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from core.safety import ComplianceChecker, SafetyGuard
from core.state_manager import StateManager

_JST = ZoneInfo("Asia/Tokyo")


class TestCanPostNormal:
    """can_post should return True under normal conditions."""

    def test_can_post_normal(self, safety_guard: SafetyGuard) -> None:
        # Patch datetime.now so we are within active hours
        fake_now = datetime(2026, 3, 22, 12, 0, 0)
        with patch("core.safety.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            allowed, reason = safety_guard.can_post()
        assert allowed is True, f"Normal conditions should allow posting, got reason={reason}"
        assert reason == "ok"


class TestCanPostEmergencyStop:
    """can_post should return False when emergency stop is active."""

    def test_can_post_emergency_stop(self, safety_guard: SafetyGuard) -> None:
        # Activate emergency stop
        safety_guard.emergency_stop("test stop")
        allowed, reason = safety_guard.can_post()
        assert allowed is False, "Emergency stop should block posting"
        assert "emergency_stop" in reason, f"Reason should mention emergency_stop, got: {reason}"


class TestCanPostDailyLimit:
    """can_post should return False when the daily post limit is reached."""

    def test_can_post_daily_limit(self, safety_guard: SafetyGuard) -> None:
        max_daily = safety_guard.config["max_daily_posts"]
        fake_now = datetime(2026, 3, 22, 12, 0, 0)

        with patch("core.safety.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            # Record posts up to the limit
            for _ in range(max_daily):
                safety_guard.record_post()

            allowed, reason = safety_guard.can_post()

        assert allowed is False, "Should be blocked after reaching daily limit"
        assert "daily_limit_reached" in reason, f"Reason should mention daily_limit_reached, got: {reason}"


class TestCanPostInterval:
    """can_post should return False when the minimum interval has not elapsed."""

    def test_can_post_interval(self, safety_guard: SafetyGuard) -> None:
        min_interval = safety_guard.config["min_post_interval_minutes"]
        # Write a post_history entry with a very recent JST-aware timestamp
        recent_time = datetime(2026, 3, 22, 12, 0, 0, tzinfo=_JST)
        history = {
            "posts": [{"posted_at": recent_time.isoformat()}],
        }
        safety_guard.sm.save_json("post_history.json", history)

        # "now" is only 1 minute after the last post
        fake_now = recent_time + timedelta(minutes=1)
        with patch("core.safety.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            allowed, reason = safety_guard.can_post()

        assert allowed is False, "Should be blocked when interval has not elapsed"
        assert "min_interval" in reason, f"Reason should mention min_interval, got: {reason}"


class TestEmergencyStopAndClear:
    """emergency_stop and clear_emergency_stop should toggle the flag."""

    def test_emergency_stop_and_clear(self, safety_guard: SafetyGuard) -> None:
        # Activate
        safety_guard.emergency_stop("unit test")
        state = safety_guard.sm.load_json("system_state.json")
        assert state["emergency_stop"] is True, "emergency_stop flag should be True after activation"
        assert state["emergency_stop_reason"] == "unit test"

        # Clear
        safety_guard.clear_emergency_stop()
        state = safety_guard.sm.load_json("system_state.json")
        assert state["emergency_stop"] is False, "emergency_stop flag should be False after clearing"
        assert state["emergency_stop_reason"] is None


class TestRecordErrorCircuitBreaker:
    """Consecutive errors should trigger the circuit breaker."""

    def test_record_error_circuit_breaker(self, safety_guard: SafetyGuard) -> None:
        max_errors = safety_guard.config["max_consecutive_errors"]
        agent = "test_agent"

        # Record errors below the threshold — circuit should remain closed
        for i in range(max_errors - 1):
            safety_guard.record_error(agent, f"error {i}")

        # Patch datetime so the last_error_at is recent (within cooldown)
        fake_now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=_JST)
        with patch("core.safety.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            assert safety_guard.is_circuit_open(agent) is False, (
                f"Circuit should be closed with {max_errors - 1} errors"
            )

            # One more error to hit the threshold
            safety_guard.record_error(agent, "final error")

            assert safety_guard.is_circuit_open(agent) is True, (
                f"Circuit should be open after {max_errors} consecutive errors"
            )


# ======================================================================
# ComplianceChecker tests
# ======================================================================


class TestComplianceCheckerSafe:
    """ComplianceChecker should return safe for compliant content."""

    def test_safe_verdict(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "safe",
            "reason": "56項目��範囲内の表現です",
            "flagged_expressions": [],
        })
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("セラミドは肌のうるおいを保つ成分です")
        assert result["verdict"] == "safe"
        assert result["review_required"] is False
        assert result["flagged_expressions"] == []


class TestComplianceCheckerViolation:
    """ComplianceChecker should return violation for non-compliant content."""

    def test_violation_verdict(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "violation",
            "reason": "「シミが消える」は化粧品の効能範囲外です",
            "flagged_expressions": ["シミが消える"],
        })
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("この美容液でシミが消える")
        assert result["verdict"] == "violation"
        assert result["review_required"] is True
        assert "シミが消える" in result["flagged_expressions"]


class TestComplianceCheckerBorderline:
    """ComplianceChecker should return borderline for ambiguous content."""

    def test_borderline_verdict(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "borderline",
            "reason": "「透明感が出る」は解釈次第で範囲外となる可能性",
            "flagged_expressions": ["透明感が出る"],
        })
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("この化粧水で透明感が出る")
        assert result["verdict"] == "borderline"
        assert result["review_required"] is True


class TestComplianceCheckerJsonFallback:
    """ComplianceChecker should fall back to borderline on invalid JSON."""

    def test_invalid_json_falls_back(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = "This is not valid JSON at all."
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("テスト文")
        assert result["verdict"] == "borderline"
        assert result["review_required"] is True

    def test_json_with_surrounding_text(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = (
            'Here is the result: {"verdict": "safe", '
            '"reason": "問題なし", "flagged_expressions": []} end'
        )
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("保湿は大切です")
        assert result["verdict"] == "safe"
        assert result["review_required"] is False


class TestComplianceCheckerApiError:
    """ComplianceChecker should fall back to borderline on API errors."""

    def test_api_error_falls_back(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.side_effect = RuntimeError("CLI timeout")
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("テスト文")
        assert result["verdict"] == "borderline"
        assert result["review_required"] is True


class TestComplianceCheckerUnknownVerdict:
    """ComplianceChecker should normalise unknown verdicts to borderline."""

    def test_unknown_verdict_normalised(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "maybe_safe",
            "reason": "不明な判定",
            "flagged_expressions": [],
        })
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("テスト文")
        assert result["verdict"] == "borderline"
        assert result["review_required"] is True


class TestComplianceCheckerBuildPrompt:
    """_build_prompt should correctly substitute placeholders."""

    def test_placeholders_replaced(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "safe", "reason": "", "flagged_expressions": [],
        })
        checker = ComplianceChecker(claude_client=mock_client)
        checker.check("テスト投稿文")
        # Verify the prompt passed to client.prompt
        call_args = mock_client.prompt.call_args
        prompt_text = call_args[0][0]
        assert "{claims_56}" not in prompt_text, "Placeholder {claims_56} should be replaced"
        assert "{content}" not in prompt_text, "Placeholder {content} should be replaced"
        assert "テスト投稿文" in prompt_text, "Content should appear in the prompt"
        assert "化粧品の効能の範囲" in prompt_text, "56 claims should be injected"


class TestComplianceCheckerMinimalJson:
    """_parse_response should handle JSON with missing optional keys."""

    def test_missing_reason_and_flagged(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({"verdict": "safe"})
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("テスト文")
        assert result["verdict"] == "safe"
        assert result["reason"] == ""
        assert result["flagged_expressions"] == []

    def test_missing_verdict_defaults_borderline(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({"reason": "不明"})
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("テスト文")
        assert result["verdict"] == "borderline"
        assert result["review_required"] is True

    def test_flagged_expressions_non_list_normalised(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "safe",
            "reason": "",
            "flagged_expressions": "not a list",
        })
        checker = ComplianceChecker(claude_client=mock_client)
        result = checker.check("テスト文")
        assert result["flagged_expressions"] == [], (
            "Non-list flagged_expressions should be normalised to []"
        )


class TestComplianceCheckerPromptMissing:
    """ComplianceChecker should fall back to borderline if template is missing."""

    def test_missing_template_falls_back(self, tmp_path) -> None:
        mock_client = MagicMock()
        checker = ComplianceChecker(claude_client=mock_client)
        # Point to a nonexistent path
        checker._PROMPT_PATH = tmp_path / "nonexistent.txt"
        result = checker.check("テスト文")
        assert result["verdict"] == "borderline"
        assert result["review_required"] is True
        mock_client.prompt.assert_not_called()
