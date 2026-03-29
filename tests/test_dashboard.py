"""Tests for scripts/dashboard.py — P2-4."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from core.state_manager import StateManager


@pytest.fixture()
def sm(tmp_path: Path) -> StateManager:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return StateManager(state_dir=state_dir)


def _seed_state(sm: StateManager) -> None:
    """Populate state files with minimal data for dashboard rendering."""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today = now.strftime("%Y-%m-%d")

    sm.save_json("system_state.json", {
        "emergency_stop": False,
        "last_health_check": now.isoformat(),
        "agent_status": {
            "writer": {"last_run": now.isoformat(), "status": "ok"},
            "poster": {"last_run": now.isoformat(), "status": "ok"},
        },
        "daily_counters": {today: {"posts": 3}},
    })
    sm.save_json("draft_queue.json", {
        "drafts": [{"status": "pending_review"}, {"status": "approved"}],
    })
    sm.save_json("post_queue.json", {
        "queue": [
            {"status": "pending", "scheduled_at": now.isoformat()},
            {"status": "posted"},
        ],
    })
    sm.save_json("research_pool.json", {
        "items": [
            {"used": False}, {"used": False}, {"used": True},
        ],
    })
    sm.save_json("performance.json", {
        "summary": {
            "avg_engagement_rate": 3.5,
            "top_category": "skincare_knowledge",
        },
    })
    sm.save_json("token_state.json", {
        "expires_at": (now + datetime.timedelta(days=50)).isoformat(),
    })


class TestRenderDashboard:
    def test_renders_without_error(self, sm: StateManager):
        _seed_state(sm)
        from scripts.dashboard import render_dashboard
        output = render_dashboard(sm)
        assert isinstance(output, str)
        assert len(output) > 50

    def test_shows_running_status(self, sm: StateManager):
        _seed_state(sm)
        from scripts.dashboard import render_dashboard
        output = render_dashboard(sm)
        assert "RUNNING" in output

    def test_shows_stopped_when_emergency(self, sm: StateManager):
        _seed_state(sm)
        state = sm.load_json("system_state.json")
        state["emergency_stop"] = True
        sm.save_json("system_state.json", state)

        from scripts.dashboard import render_dashboard
        output = render_dashboard(sm)
        assert "STOPPED" in output

    def test_shows_queue_counts(self, sm: StateManager):
        _seed_state(sm)
        from scripts.dashboard import render_dashboard
        output = render_dashboard(sm)
        # Should contain draft, post, research queue info
        assert "draft" in output.lower() or "Draft" in output

    def test_shows_agent_status(self, sm: StateManager):
        _seed_state(sm)
        from scripts.dashboard import render_dashboard
        output = render_dashboard(sm)
        assert "writer" in output.lower()
        assert "poster" in output.lower()

    def test_empty_state_no_crash(self, sm: StateManager):
        from scripts.dashboard import render_dashboard
        output = render_dashboard(sm)
        assert isinstance(output, str)
        assert "RUNNING" in output or "Status" in output
