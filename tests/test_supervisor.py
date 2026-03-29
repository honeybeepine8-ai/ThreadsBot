"""Integration tests for SupervisorAgent — health checks and anomaly detection."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager
from core.safety import SafetyGuard
from agents.supervisor import SupervisorAgent

_JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture()
def supervisor_env(tmp_path: Path):
    """Set up a SupervisorAgent with temp state directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    sm = StateManager(state_dir=state_dir)

    now = datetime.datetime.now(_JST)

    sm.save_json("system_state.json", {
        "emergency_stop": False,
        "emergency_stop_reason": None,
        "emergency_stop_at": None,
        "last_health_check": None,
        "agent_status": {
            "poster": {"last_run": now.isoformat(), "status": "ok", "error": None},
            "writer": {"last_run": now.isoformat(), "status": "ok", "error": None},
            "researcher": {"last_run": now.isoformat(), "status": "ok", "error": None},
            "fetcher": {"last_run": now.isoformat(), "status": "ok", "error": None},
            "analyst": {"last_run": now.isoformat(), "status": "ok", "error": None},
        },
        "daily_counters": {},
    })
    sm.save_json("post_history.json", {
        "last_updated": None,
        "posts": [],
        "daily_stats": {},
    })

    env_vars = {
        "THREADS_ACCESS_TOKEN": "test_token",
        "THREADS_USER_ID": "test_user",
    }

    with patch.dict("os.environ", env_vars):
        with patch("agents.base_agent.SafetyGuard") as mock_sg_cls:
            sg = SafetyGuard(sm)
            mock_sg_cls.return_value = sg

            agent = SupervisorAgent()
            agent.state = sm
            agent.safety = sg
            yield agent, sm


class TestAgentHealth:
    """Tests for _check_agent_health."""

    def test_no_alerts_when_healthy(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        alerts = agent._check_agent_health()
        assert len(alerts) == 0

    def test_detects_stale_agent(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        old = (datetime.datetime.now(_JST) - datetime.timedelta(hours=10)).isoformat()
        state = sm.load_json("system_state.json")
        state["agent_status"]["poster"]["last_run"] = old
        sm.save_json("system_state.json", state)

        alerts = agent._check_agent_health()
        stale_alerts = [a for a in alerts if "stale" in a]
        assert len(stale_alerts) >= 1
        assert "poster" in stale_alerts[0]

    def test_detects_error_status(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        state = sm.load_json("system_state.json")
        state["agent_status"]["writer"]["status"] = "error"
        state["agent_status"]["writer"]["error"] = "API timeout"
        sm.save_json("system_state.json", state)

        alerts = agent._check_agent_health()
        error_alerts = [a for a in alerts if "error state" in a]
        assert len(error_alerts) >= 1


class TestPostingAnomalies:
    """Tests for _check_posting_anomalies."""

    def test_no_anomalies_with_proper_spacing(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        now = datetime.datetime.now(_JST)
        posts = []
        for i in range(3):
            dt = now - datetime.timedelta(hours=3 * i)
            posts.append({
                "id": f"post_test_{i}",
                "posted_at": dt.isoformat(),
                "category": ["skincare_knowledge", "beauty_trend", "skincare_routine"][i],
            })
        sm.save_json("post_history.json", {"posts": posts, "daily_stats": {}})

        alerts = agent._check_posting_anomalies()
        assert len(alerts) == 0

    def test_detects_close_interval(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        now = datetime.datetime.now(_JST)
        posts = [
            {"id": "p1", "posted_at": (now - datetime.timedelta(minutes=10)).isoformat(), "category": "a"},
            {"id": "p2", "posted_at": now.isoformat(), "category": "b"},
        ]
        sm.save_json("post_history.json", {"posts": posts, "daily_stats": {}})

        alerts = agent._check_posting_anomalies()
        interval_alerts = [a for a in alerts if "close together" in a]
        assert len(interval_alerts) >= 1

    def test_detects_category_streak(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        now = datetime.datetime.now(_JST)
        posts = []
        for i in range(6):
            dt = now - datetime.timedelta(hours=i)
            posts.append({
                "id": f"p_{i}",
                "posted_at": dt.isoformat(),
                "category": "skincare_knowledge",
            })
        sm.save_json("post_history.json", {"posts": posts, "daily_stats": {}})

        alerts = agent._check_posting_anomalies()
        streak_alerts = [a for a in alerts if "streak" in a.lower()]
        assert len(streak_alerts) >= 1


class TestErrorRate:
    """Tests for _check_error_rate."""

    def test_no_alert_on_low_error_rate(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        today = datetime.datetime.now(_JST).strftime("%Y-%m-%d")
        state = sm.load_json("system_state.json")
        state["daily_counters"] = {
            today: {"posts_published": 5, "api_errors": 1}
        }
        sm.save_json("system_state.json", state)

        alerts = agent._check_error_rate()
        assert len(alerts) == 0

    def test_alert_on_high_error_rate(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        today = datetime.datetime.now(_JST).strftime("%Y-%m-%d")
        state = sm.load_json("system_state.json")
        state["daily_counters"] = {
            today: {"posts_published": 2, "api_errors": 5}
        }
        sm.save_json("system_state.json", state)

        alerts = agent._check_error_rate()
        assert len(alerts) >= 1
        assert "error rate" in alerts[0].lower() or "API error" in alerts[0]


class TestTokenExpiryCheck:
    """Tests for _check_token_expiry in SupervisorAgent."""

    def test_no_alert_when_token_valid(self, supervisor_env) -> None:
        agent, sm = supervisor_env
        now_jst = datetime.datetime.now(_JST)
        future = (now_jst + datetime.timedelta(days=30)).isoformat()
        sm.save_json("token_state.json", {
            "access_token": "valid_token",
            "refreshed_at": now_jst.isoformat(),
            "expires_at": future,
        })

        alerts = agent._check_token_expiry()
        assert len(alerts) == 0

    def test_token_expiring_soon_triggers_refresh(self, supervisor_env) -> None:
        """When token is expiring soon, Supervisor should call get_valid_token()."""
        agent, sm = supervisor_env
        now_jst = datetime.datetime.now(_JST)
        soon = (now_jst + datetime.timedelta(days=3)).isoformat()
        sm.save_json("token_state.json", {
            "access_token": "expiring_token",
            "refreshed_at": now_jst.isoformat(),
            "expires_at": soon,
        })

        with patch("agents.supervisor.TokenManager") as mock_tm_cls:
            mock_tm = MagicMock()
            mock_tm.get_token_status.return_value = {
                "status": "expiring_soon",
                "days_remaining": 3,
                "expires_at": soon,
                "needs_refresh": True,
            }
            mock_tm.get_valid_token.return_value = "new_refreshed_token"
            mock_tm_cls.return_value = mock_tm

            alerts = agent._check_token_expiry()

        # Refresh should have been called
        mock_tm.get_valid_token.assert_called_once()
        # No alert (refresh succeeded)
        assert len(alerts) == 0

    def test_token_refresh_failure_sends_alert(self, supervisor_env) -> None:
        """When token refresh fails, alert should be returned and notifier called."""
        agent, sm = supervisor_env
        soon = (datetime.datetime.now(_JST) + datetime.timedelta(days=3)).isoformat()

        with patch("agents.supervisor.TokenManager") as mock_tm_cls:
            mock_tm = MagicMock()
            mock_tm.get_token_status.return_value = {
                "status": "expiring_soon",
                "days_remaining": 3,
                "expires_at": soon,
                "needs_refresh": True,
            }
            mock_tm.get_valid_token.side_effect = Exception("Network error")
            mock_tm_cls.return_value = mock_tm

            with patch.object(agent.notifier, "send") as mock_notify:
                alerts = agent._check_token_expiry()

        # Token alerts are sent directly via Notifier (not added to alerts list)
        assert len(alerts) == 0
        mock_notify.assert_any_call(
            "token_refresh_failed",
            ANY,
            "critical",
        )

    def test_token_expired_sends_critical_notification(self, supervisor_env) -> None:
        """When token is expired, critical notification should be sent."""
        agent, sm = supervisor_env
        now_jst = datetime.datetime.now(_JST)
        past = (now_jst - datetime.timedelta(days=1)).isoformat()
        sm.save_json("token_state.json", {
            "access_token": "expired_token",
            "refreshed_at": now_jst.isoformat(),
            "expires_at": past,
        })

        with patch.object(agent.notifier, "send") as mock_notify:
            alerts = agent._check_token_expiry()

        # Token alerts are sent directly via Notifier (not added to alerts list)
        assert len(alerts) == 0
        mock_notify.assert_called_once_with(
            "token_expired",
            ANY,
            "critical",
        )

    def test_no_alert_when_token_unknown(self, supervisor_env) -> None:
        """When token state is untracked, no alert or notification is sent."""
        agent, sm = supervisor_env
        # Empty token state → status == "unknown"
        sm.save_json("token_state.json", {})

        with patch.object(agent.notifier, "send") as mock_notify:
            alerts = agent._check_token_expiry()

        assert len(alerts) == 0
        mock_notify.assert_not_called()
