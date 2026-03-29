"""Notifier integration tests — agent notification flows (P4-3).

Tests that agents correctly call Notifier for various events.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager
from core.notifier import SEVERITY_MEDIUM, SEVERITY_HIGH, SEVERITY_CRITICAL

_JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture()
def sm(tmp_path: Path) -> StateManager:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return StateManager(state_dir=state_dir)


class TestSupervisorNotification:
    """Supervisor sends alerts via Notifier."""

    def test_single_alert_sends_notification(self, sm: StateManager):
        now = datetime.datetime.now(_JST)
        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "agent_status": {
                "writer": {
                    "last_run": (now - datetime.timedelta(hours=10)).isoformat(),
                    "status": "ok",
                },
            },
            "daily_counters": {},
        })
        sm.save_json("post_history.json", {"posts": []})
        sm.save_json("token_state.json", {
            "expires_at": (now + datetime.timedelta(days=50)).isoformat(),
        })

        from agents.supervisor import SupervisorAgent
        with patch.object(SupervisorAgent, "__init__", lambda self: None):
            sup = SupervisorAgent.__new__(SupervisorAgent)
            sup.name = "supervisor"
            sup.state = sm
            from core.logger import get_logger
            sup.logger = get_logger("supervisor")
            from core.safety import SafetyGuard
            sup.safety = SafetyGuard(sm)
            sup.config = {}
            from core.notifier import Notifier
            sup.notifier = MagicMock(spec=Notifier)

        sup.execute()

        # Supervisor should have called notifier.send for the stale writer alert
        assert sup.notifier.send.call_count >= 1

    def test_three_alerts_trigger_emergency(self, sm: StateManager):
        now = datetime.datetime.now(_JST)
        # Setup: multiple agents stale + high error rate
        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "agent_status": {
                "writer": {
                    "last_run": (now - datetime.timedelta(hours=10)).isoformat(),
                    "status": "error",
                    "error": "test error",
                },
                "poster": {
                    "last_run": (now - datetime.timedelta(hours=5)).isoformat(),
                    "status": "error",
                    "error": "test error",
                },
                "fetcher": {
                    "last_run": (now - datetime.timedelta(hours=20)).isoformat(),
                    "status": "error",
                    "error": "test error",
                },
            },
            "daily_counters": {},
        })
        sm.save_json("post_history.json", {"posts": []})
        sm.save_json("token_state.json", {
            "expires_at": (now + datetime.timedelta(days=50)).isoformat(),
        })

        from agents.supervisor import SupervisorAgent
        with patch.object(SupervisorAgent, "__init__", lambda self: None):
            sup = SupervisorAgent.__new__(SupervisorAgent)
            sup.name = "supervisor"
            sup.state = sm
            from core.logger import get_logger
            sup.logger = get_logger("supervisor")
            from core.safety import SafetyGuard
            sup.safety = SafetyGuard(sm)
            sup.config = {}
            from core.notifier import Notifier
            sup.notifier = MagicMock(spec=Notifier)

        sup.execute()

        # Should have sent emergency_stop notification
        call_args_list = sup.notifier.send.call_args_list
        event_types = [c[0][0] for c in call_args_list]
        assert "emergency_stop" in event_types


class TestSupervisorAlertSeverity:
    """_classify_alert_severity assigns correct levels."""

    def test_expired_token_is_critical(self):
        from agents.supervisor import SupervisorAgent
        assert SupervisorAgent._classify_alert_severity("Token has EXPIRED") == SEVERITY_CRITICAL

    def test_error_rate_is_high(self):
        from agents.supervisor import SupervisorAgent
        assert SupervisorAgent._classify_alert_severity("High API error rate") == SEVERITY_HIGH

    def test_stale_agent_is_medium(self):
        from agents.supervisor import SupervisorAgent
        assert SupervisorAgent._classify_alert_severity("Agent 'writer' is stale") == SEVERITY_MEDIUM


class TestPosterNotification:
    """Poster sends notification on API failure."""

    def test_post_failure_notifies(self, sm: StateManager):
        now = datetime.datetime.now(_JST)
        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "daily_counters": {},
        })
        sm.save_json("post_queue.json", {
            "queue": [{
                "id": "q_test_001",
                "content": "test",
                "scheduled_at": (now - datetime.timedelta(minutes=1)).isoformat(),
                "status": "pending",
            }],
        })
        sm.save_json("post_history.json", {"posts": []})

        from agents.poster import PosterAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        mock_threads = MagicMock()
        mock_threads.create_text_post.side_effect = Exception("API down")

        with patch.object(PosterAgent, "__init__", lambda self: None):
            poster = PosterAgent.__new__(PosterAgent)
            poster.name = "poster"
            poster.state = sm
            poster.logger = get_logger("poster")
            poster.safety = SafetyGuard(sm)
            poster.threads = mock_threads
            poster.notifier = MagicMock(spec=Notifier)

        with pytest.raises(Exception, match="API down"):
            poster.execute()

        # Should have notified about the failure
        poster.notifier.send.assert_called_once()
        args = poster.notifier.send.call_args[0]
        assert args[0] == "post_failed"
        assert SEVERITY_HIGH == args[2]
