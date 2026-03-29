"""Tests for agents.cross_poster.CrossPosterAgent."""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from core.account_context import AccountContext
from core.state_manager import StateManager

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime.datetime(2026, 4, 1, 14, 0, 0, tzinfo=_JST)
_PROJECT_ROOT = AccountContext.PROJECT_ROOT


def _make_post(post_id: str, media_id: str, hours_ago: int = 24, views: int = 500, likes: int = 30) -> dict:
    posted_at = (_NOW - datetime.timedelta(hours=hours_ago)).isoformat()
    return {
        "id": post_id,
        "media_id": media_id,
        "content": "テスト投稿です。成分表を見てみて。",
        "category": "skincare_knowledge",
        "posted_at": posted_at,
        "metrics": {
            "views": views,
            "likes": likes,
            "replies": 5,
            "reposts": 3,
        },
    }


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def cross_poster_env(tmp_path: Path):
    """Set up a CrossPosterAgent with mocked APIs and temp dirs."""
    # Create "other" account directory
    other_dir = _PROJECT_ROOT / "accounts" / "test_other_acct"
    if other_dir.exists():
        shutil.rmtree(other_dir)
    shutil.copytree(_PROJECT_ROOT / "accounts" / "_template", other_dir)
    (other_dir / "data" / "state").mkdir(parents=True, exist_ok=True)

    # Save a post_history for the other account
    other_sm = StateManager(state_dir=other_dir / "data" / "state")
    other_sm.save_json("post_history.json", {
        "posts": [_make_post("p1", "m1", hours_ago=24, views=500, likes=30)],
    })

    # State dir for "self" (default account)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sm = StateManager(state_dir=state_dir)
    sm.save_json("system_state.json", {"emergency_stop": False, "agent_status": {}})
    sm.save_json("cross_post_state.json", {"cross_posts": [], "daily_counts": {}})

    env_vars = {
        "THREADS_ACCESS_TOKEN": "test_token",
        "THREADS_USER_ID": "test_user",
    }

    with patch.dict("os.environ", env_vars), \
         patch("agents.cross_poster.ThreadsAPIClient") as mock_api_cls, \
         patch("agents.cross_poster.ClaudeClient") as mock_claude_cls:
        mock_api = MagicMock()
        mock_api.create_text_post.return_value = {"id": "cross_media_001"}
        mock_api_cls.return_value = mock_api

        mock_claude = MagicMock()
        mock_claude.generate_post.return_value = "成分的にもこれ正しくて、セラミドNPが上位にあるのがポイントなんだよね"
        mock_claude_cls.return_value = mock_claude

        from agents.cross_poster import CrossPosterAgent
        agent = CrossPosterAgent()
        agent.state = sm
        # Ensure config is loaded
        agent._config["enabled"] = True

        yield agent, sm, mock_api, mock_claude

    # Cleanup
    if other_dir.exists():
        shutil.rmtree(other_dir)


# ------------------------------------------------------------------
# Config and init tests
# ------------------------------------------------------------------


class TestCrossPosterInit:

    def test_disabled_by_default_when_no_config(self, tmp_path: Path):
        env_vars = {"THREADS_ACCESS_TOKEN": "t", "THREADS_USER_ID": "u"}
        with patch.dict("os.environ", env_vars), \
             patch("agents.cross_poster.ThreadsAPIClient"), \
             patch("agents.cross_poster.ClaudeClient"), \
             patch("agents.cross_poster._PROJECT_ROOT", tmp_path):
            from agents.cross_poster import CrossPosterAgent
            agent = CrossPosterAgent()
            assert not agent._config.get("enabled", False)

    def test_loads_config_values(self, cross_poster_env):
        agent, *_ = cross_poster_env
        assert agent._min_views == 200
        assert agent._cross_post_rate == 0.15
        assert agent._max_daily == 3


# ------------------------------------------------------------------
# Account discovery tests
# ------------------------------------------------------------------


class TestGetOtherAccounts:

    def test_excludes_self(self, cross_poster_env):
        agent, *_ = cross_poster_env
        others = agent._get_other_accounts()
        assert agent.ctx.account_id not in others

    def test_excludes_template(self, cross_poster_env):
        agent, *_ = cross_poster_env
        others = agent._get_other_accounts()
        assert "_template" not in others


# ------------------------------------------------------------------
# Candidate selection tests
# ------------------------------------------------------------------


class TestFindCandidates:

    def test_finds_high_engagement_posts(self, cross_poster_env):
        agent, sm, *_ = cross_poster_env
        cp_state = sm.load_json("cross_post_state.json")

        candidates = agent._find_candidates(["test_other_acct"], cp_state, _NOW)
        assert len(candidates) >= 1
        assert candidates[0]["source_account"] == "test_other_acct"
        assert candidates[0]["source_post_id"] == "p1"

    def test_skips_already_quoted(self, cross_poster_env):
        agent, sm, *_ = cross_poster_env
        cp_state = {
            "cross_posts": [{"source_post_id": "p1", "pair_key": "test_other_acct->default"}],
            "daily_counts": {},
        }
        candidates = agent._find_candidates(["test_other_acct"], cp_state, _NOW)
        assert len(candidates) == 0

    def test_skips_low_engagement(self, cross_poster_env):
        agent, sm, *_ = cross_poster_env
        # Override the other account's post with low views
        other_sm = AccountContext("test_other_acct").get_state_manager()
        other_sm.save_json("post_history.json", {
            "posts": [_make_post("p_low", "m_low", views=10, likes=0)],
        })

        cp_state = sm.load_json("cross_post_state.json")
        candidates = agent._find_candidates(["test_other_acct"], cp_state, _NOW)
        assert len(candidates) == 0


# ------------------------------------------------------------------
# Pair limits tests
# ------------------------------------------------------------------


class TestCheckPairLimits:

    def test_allows_first_cross_post(self, cross_poster_env):
        agent, *_ = cross_poster_env
        cp_state = {"cross_posts": [], "daily_counts": {}}
        assert agent._check_pair_limits("a->b", cp_state, _NOW) is True

    def test_blocks_within_interval(self, cross_poster_env):
        agent, *_ = cross_poster_env
        recent = (_NOW - datetime.timedelta(hours=2)).isoformat()
        cp_state = {
            "cross_posts": [{"pair_key": "a->b", "created_at": recent}],
        }
        assert agent._check_pair_limits("a->b", cp_state, _NOW) is False

    def test_allows_after_interval(self, cross_poster_env):
        agent, *_ = cross_poster_env
        old = (_NOW - datetime.timedelta(hours=48)).isoformat()
        cp_state = {
            "cross_posts": [{"pair_key": "a->b", "created_at": old}],
        }
        assert agent._check_pair_limits("a->b", cp_state, _NOW) is True


# ------------------------------------------------------------------
# Execute flow tests
# ------------------------------------------------------------------


class TestExecute:

    def test_skips_when_disabled(self, cross_poster_env):
        agent, sm, mock_api, mock_claude = cross_poster_env
        agent._config["enabled"] = False

        with patch("agents.cross_poster.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            agent.execute()

        mock_api.create_text_post.assert_not_called()

    def test_skips_outside_active_hours(self, cross_poster_env):
        agent, sm, mock_api, *_ = cross_poster_env
        late_night = _NOW.replace(hour=3)

        with patch("agents.cross_poster.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = late_night
            agent.execute()

        mock_api.create_text_post.assert_not_called()

    def test_skips_when_daily_limit_reached(self, cross_poster_env):
        agent, sm, mock_api, *_ = cross_poster_env
        today = _NOW.strftime("%Y-%m-%d")
        sm.save_json("cross_post_state.json", {
            "cross_posts": [],
            "daily_counts": {today: 3},  # Already at limit
        })

        with patch("agents.cross_poster.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            agent.execute()

        mock_api.create_text_post.assert_not_called()
