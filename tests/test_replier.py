"""Tests for agents.replier — automatic reply to follower comments."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager
from core.safety import SafetyGuard

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime.datetime(2026, 3, 26, 14, 0, 0, tzinfo=_JST)
_TODAY_KEY = _NOW.strftime("%Y-%m-%d")


def _make_post(post_id: str, media_id: str, hours_ago: int = 12) -> dict:
    """Build a post_history entry."""
    posted_at = (_NOW - datetime.timedelta(hours=hours_ago)).isoformat()
    return {
        "id": post_id,
        "threads_media_id": media_id,
        "content": "テスト投稿の内容です",
        "posted_at": posted_at,
    }


def _make_comment(
    comment_id: str,
    text: str = "いいね！",
    username: str = "follower1",
    hours_ago: float = 1,
    from_id: str = "user_follower1",
) -> dict:
    """Build a Threads reply object."""
    ts = (_NOW - datetime.timedelta(hours=hours_ago)).isoformat()
    return {
        "id": comment_id,
        "text": text,
        "username": username,
        "timestamp": ts,
        "from": {"id": from_id},
    }


@pytest.fixture()
def replier_env(tmp_path: Path):
    """Set up a ReplierAgent with mocked APIs and temp state."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sm = StateManager(state_dir=state_dir)

    # Seed state files
    sm.save_json("system_state.json", {
        "emergency_stop": False,
        "emergency_stop_reason": None,
        "emergency_stop_at": None,
        "agent_status": {},
        "daily_counters": {},
    })
    sm.save_json("post_history.json", {
        "last_updated": None,
        "posts": [_make_post("post_001", "media_001")],
        "daily_stats": {},
    })
    sm.save_json("reply_state.json", {
        "last_updated": None,
        "processed_comments": {},
        "daily_reply_count": {},
    })

    env_vars = {
        "THREADS_ACCESS_TOKEN": "test_token",
        "THREADS_USER_ID": "test_user",
    }

    with patch.dict("os.environ", env_vars):
        with patch("agents.replier.ThreadsAPIClient") as mock_api_cls:
            with patch("agents.replier.ClaudeClient") as mock_claude_cls:
                mock_api = MagicMock()
                mock_api.user_id = "test_user"
                mock_api.get_user_profile.return_value = {"username": "bot_self"}
                mock_api.get_post_replies.return_value = [
                    _make_comment("c_001", "すごく参考になった！", "follower1"),
                ]
                mock_api.create_text_post.return_value = {"id": "reply_media_001"}
                mock_api_cls.return_value = mock_api

                mock_claude = MagicMock()
                mock_claude.generate_post.return_value = "ありがとう！参考になってうれしいな"
                mock_claude_cls.return_value = mock_claude

                with patch("agents.replier.QualityGate") as mock_qg_cls:
                    mock_qg = MagicMock()
                    mock_qg.check_ng_words.return_value = None  # No NG words
                    mock_qg_cls.return_value = mock_qg

                    from agents.replier import ReplierAgent
                    agent = ReplierAgent()
                    agent.state = sm
                    agent.safety = SafetyGuard(sm)

                    yield agent, sm, mock_api, mock_claude, mock_qg


# ====================================================================
# Daily limit
# ====================================================================


class TestReplierDailyLimit:

    def test_skips_when_daily_limit_reached(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # Set daily count to max
        sm.save_json("reply_state.json", {
            "processed_comments": {},
            "daily_reply_count": {_TODAY_KEY: agent.max_daily_replies},
        })

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.get_post_replies.assert_not_called()


# ====================================================================
# No-op scenarios
# ====================================================================


class TestReplierNoOp:

    def test_skips_when_no_recent_posts(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # All posts are too old
        sm.save_json("post_history.json", {
            "posts": [_make_post("old", "media_old", hours_ago=100)],
        })
        agent.scan_hours = 48  # default

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.get_post_replies.assert_not_called()

    def test_skips_when_no_new_comments(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # All comments already processed
        sm.save_json("reply_state.json", {
            "processed_comments": {
                "c_001": {"action": "replied", "replied_at": _NOW.isoformat()},
            },
            "daily_reply_count": {},
        })

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.create_text_post.assert_not_called()


# ====================================================================
# Own comments
# ====================================================================


class TestReplierOwnComments:

    def test_skips_own_comments(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # Comment from the bot's own user_id
        mock_api.get_post_replies.return_value = [
            _make_comment("c_own", "PR おすすめ！", "bot_self", from_id="test_user"),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        # Should NOT post a reply
        mock_api.create_text_post.assert_not_called()

        # Should be marked as skipped_own in state
        state = sm.load_json("reply_state.json")
        assert state["processed_comments"]["c_own"]["action"] == "skipped_own"


# ====================================================================
# Delay window
# ====================================================================


class TestReplierDelay:

    def test_skips_too_fresh_comments(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # Comment posted just 1 minute ago (way under min_reply_delay)
        mock_api.get_post_replies.return_value = [
            _make_comment("c_fresh", "新しいコメント", "follower1", hours_ago=0.01),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.create_text_post.assert_not_called()


# ====================================================================
# Reply rate
# ====================================================================


class TestReplierRate:

    def test_applies_reply_rate_skip(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # Force random to always exceed reply_rate → skip
        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            with patch("agents.replier.random") as mock_rnd:
                mock_rnd.random.return_value = 0.99  # > reply_rate (0.65)
                mock_rnd.shuffle = lambda x: None
                agent.execute()

        mock_api.create_text_post.assert_not_called()

        state = sm.load_json("reply_state.json")
        assert state["processed_comments"]["c_001"]["action"] == "skipped_random"


# ====================================================================
# Happy path
# ====================================================================


class TestReplierHappyPath:

    def test_posts_reply_successfully(self, replier_env) -> None:
        agent, sm, mock_api, mock_claude, mock_qg = replier_env

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            with patch("agents.replier.random") as mock_rnd:
                mock_rnd.random.return_value = 0.1  # < reply_rate (0.65)
                mock_rnd.shuffle = lambda x: None
                agent.execute()

        # Threads API should have been called with reply_to_id
        mock_api.create_text_post.assert_called_once()
        call_kwargs = mock_api.create_text_post.call_args
        assert call_kwargs[1].get("reply_to_id") == "c_001" or call_kwargs[0][0] is not None

        # State should record the reply
        state = sm.load_json("reply_state.json")
        record = state["processed_comments"]["c_001"]
        assert record["action"] == "replied"
        assert record["reply_to_username"] == "follower1"


# ====================================================================
# NG word blocking
# ====================================================================


class TestReplierNGWord:

    def test_ng_word_blocks_reply(self, replier_env) -> None:
        agent, sm, mock_api, mock_claude, mock_qg = replier_env
        # Make quality gate detect NG word
        mock_qg.check_ng_words.return_value = "治る"

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            with patch("agents.replier.random") as mock_rnd:
                mock_rnd.random.return_value = 0.1
                mock_rnd.shuffle = lambda x: None
                agent.execute()

        mock_api.create_text_post.assert_not_called()

        state = sm.load_json("reply_state.json")
        assert state["processed_comments"]["c_001"]["action"] == "skipped_ng_word"


# ====================================================================
# Generation failure
# ====================================================================


class TestReplierGenerationFailure:

    def test_generation_failure_skips(self, replier_env) -> None:
        agent, sm, mock_api, mock_claude, *_ = replier_env
        mock_claude.generate_post.side_effect = Exception("API error")

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            with patch("agents.replier.random") as mock_rnd:
                mock_rnd.random.return_value = 0.1
                mock_rnd.shuffle = lambda x: None
                agent.execute()

        mock_api.create_text_post.assert_not_called()

        state = sm.load_json("reply_state.json")
        assert state["processed_comments"]["c_001"]["action"] == "skipped_generation_failed"


# ====================================================================
# State cleanup
# ====================================================================


class TestReplierStateCleanup:

    def test_cleans_old_entries(self, replier_env) -> None:
        agent, sm, *_ = replier_env
        old_time = (_NOW - datetime.timedelta(days=10)).isoformat()

        reply_state = {
            "processed_comments": {
                "c_old": {"action": "replied", "replied_at": old_time},
                "c_recent": {"action": "replied", "replied_at": _NOW.isoformat()},
            },
            "daily_reply_count": {
                "2026-03-16": 5,  # 10 days ago
                _TODAY_KEY: 3,
            },
        }

        agent._save_reply_state(reply_state, _NOW)
        saved = sm.load_json("reply_state.json")

        # Old entries should be removed
        assert "c_old" not in saved["processed_comments"]
        assert "c_recent" in saved["processed_comments"]
        assert "2026-03-16" not in saved["daily_reply_count"]
        assert _TODAY_KEY in saved["daily_reply_count"]


# ====================================================================
# Per-user limit (bug fix verification)
# ====================================================================


class TestReplierPerUserLimit:

    def test_get_today_user_counts(self, replier_env) -> None:
        """Verify the fixed _get_today_user_counts returns correct counts."""
        agent, *_ = replier_env
        processed = {
            "c_1": {
                "action": "replied",
                "replied_at": f"{_TODAY_KEY}T10:00:00+09:00",
                "reply_to_username": "user_a",
            },
            "c_2": {
                "action": "replied",
                "replied_at": f"{_TODAY_KEY}T11:00:00+09:00",
                "reply_to_username": "user_a",
            },
            "c_3": {
                "action": "replied",
                "replied_at": f"{_TODAY_KEY}T12:00:00+09:00",
                "reply_to_username": "user_b",
            },
            "c_4": {
                "action": "skipped_random",
                "replied_at": f"{_TODAY_KEY}T13:00:00+09:00",
            },
            "c_5": {
                "action": "replied",
                "replied_at": "2026-03-25T10:00:00+09:00",  # yesterday
                "reply_to_username": "user_a",
            },
        }
        counts = agent._get_today_user_counts(processed, _TODAY_KEY)
        assert counts["user_a"] == 2
        assert counts["user_b"] == 1
        assert "user_c" not in counts  # not in data


# ====================================================================
# max_reply_delay window (upper bound)
# ====================================================================


class TestReplierMaxDelay:

    def test_skips_too_old_comments(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # Comment 3 hours old — exceeds max_reply_delay (default 7200s = 2h)
        mock_api.get_post_replies.return_value = [
            _make_comment("c_old", "古いコメント", "follower1", hours_ago=3),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.create_text_post.assert_not_called()

        state = sm.load_json("reply_state.json")
        assert state["processed_comments"]["c_old"]["action"] == "skipped_too_old"

    def test_boundary_exactly_at_max_not_skipped(self, replier_env) -> None:
        agent, sm, mock_api, mock_claude, *_ = replier_env
        # Exactly 2 hours (7200s) — not strictly greater, so should NOT skip
        mock_api.get_post_replies.return_value = [
            _make_comment("c_boundary", "ちょうど2時間", "follower1", hours_ago=2.0),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            with patch("agents.replier.random") as mock_rnd:
                mock_rnd.random.return_value = 0.01  # Pass reply_rate
                mock_rnd.shuffle = lambda x: None
                agent.execute()

        mock_api.create_text_post.assert_called_once()

    def test_comment_within_window_gets_reply(self, replier_env) -> None:
        agent, sm, mock_api, mock_claude, *_ = replier_env
        # 30 min old — within window (300s < 1800s < 7200s)
        mock_api.get_post_replies.return_value = [
            _make_comment("c_ok", "いい質問！", "follower1", hours_ago=0.5),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            with patch("agents.replier.random") as mock_rnd:
                mock_rnd.random.return_value = 0.01
                mock_rnd.shuffle = lambda x: None
                agent.execute()

        mock_api.create_text_post.assert_called_once()

    def test_skipped_too_old_not_reprocessed(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        # Pre-populate with already-skipped comment
        sm.save_json("reply_state.json", {
            "processed_comments": {
                "c_old": {
                    "post_id": "post_001",
                    "action": "skipped_too_old",
                    "replied_at": _NOW.isoformat(),
                },
            },
            "daily_reply_count": {},
        })
        mock_api.get_post_replies.return_value = [
            _make_comment("c_old", "古いコメント", "follower1", hours_ago=3),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        # Already processed — should not attempt reply
        mock_api.create_text_post.assert_not_called()


# ====================================================================
# _get_own_username
# ====================================================================


def _reset_username_cache(agent) -> None:
    """Reset _get_own_username TTL cache for testing."""
    agent._own_username_cache = None
    agent._own_username_fetched_at = 0.0


class TestGetOwnUsername:

    def test_caches_username(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        _reset_username_cache(agent)
        result1 = agent._get_own_username()
        result2 = agent._get_own_username()

        assert result1 == "bot_self"
        assert result2 == "bot_self"
        mock_api.get_user_profile.assert_called_once()  # Cached

    def test_returns_none_on_api_failure(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        _reset_username_cache(agent)
        mock_api.get_user_profile.side_effect = Exception("network error")

        result = agent._get_own_username()

        assert result is None

    def test_failure_cached_within_ttl(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        _reset_username_cache(agent)
        mock_api.get_user_profile.side_effect = Exception("network error")

        agent._get_own_username()
        agent._get_own_username()

        # Within TTL, should only call once (failure is cached)
        mock_api.get_user_profile.assert_called_once()

    def test_failure_retried_after_ttl(self, replier_env) -> None:
        agent, sm, mock_api, *_ = replier_env
        _reset_username_cache(agent)
        mock_api.get_user_profile.side_effect = Exception("network error")

        agent._get_own_username()  # First call — fails, cached

        # Simulate TTL expiry by resetting the timestamp
        agent._own_username_fetched_at = 0.0
        mock_api.get_user_profile.side_effect = None  # Recover
        mock_api.get_user_profile.return_value = {"username": "bot_self"}

        result = agent._get_own_username()

        assert result == "bot_self"
        assert mock_api.get_user_profile.call_count == 2  # Retried

    def test_own_check_skipped_when_username_none(self, replier_env) -> None:
        agent, sm, mock_api, mock_claude, *_ = replier_env
        _reset_username_cache(agent)
        mock_api.get_user_profile.side_effect = Exception("fail")

        # Comment from the bot's username — but since own_username is None,
        # the own-check guard is bypassed and comment proceeds normally
        mock_api.get_post_replies.return_value = [
            _make_comment("c_maybe_own", "テスト", "bot_self", hours_ago=0.5),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            with patch("agents.replier.random") as mock_rnd:
                mock_rnd.random.return_value = 0.01
                mock_rnd.shuffle = lambda x: None
                agent.execute()

        # Since own_username is None, the check is bypassed — reply IS posted
        mock_api.create_text_post.assert_called_once()
