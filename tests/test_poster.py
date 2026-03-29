"""Integration tests for PosterAgent — post publishing pipeline."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager
from core.safety import SafetyGuard
from agents.poster import PosterAgent

_JST = ZoneInfo("Asia/Tokyo")

# Fixed time within active hours (7:00-23:00) for deterministic tests
_FAKE_NOW = datetime.datetime(2026, 3, 26, 12, 0, 0, tzinfo=_JST)
_FAKE_PAST = (_FAKE_NOW - datetime.timedelta(hours=1)).isoformat()


@pytest.fixture()
def poster_env(tmp_path: Path):
    """Set up a PosterAgent with mocked Threads API and temp state."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    sm = StateManager(state_dir=state_dir)

    # Seed system_state with clean counters
    sm.save_json("system_state.json", {
        "emergency_stop": False,
        "emergency_stop_reason": None,
        "emergency_stop_at": None,
        "last_health_check": None,
        "agent_status": {},
        "daily_counters": {},
    })

    # Seed empty post history
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
        with patch("agents.poster.ThreadsAPIClient") as mock_api_cls:
            mock_api = MagicMock()
            mock_api.create_text_post.return_value = {
                "id": "17890012345678901",
                "success": True,
            }
            mock_api_cls.return_value = mock_api

            agent = PosterAgent()
            agent.state = sm
            agent.safety = SafetyGuard(sm)

            yield agent, sm, mock_api


def _patch_datetime_now():
    """Return a context manager that patches datetime.now in both poster and safety."""
    fake_now_naive = _FAKE_NOW.replace(tzinfo=None)

    def _mock_safety_dt_factory():
        mock_dt = MagicMock(wraps=datetime.datetime)
        mock_dt.now.return_value = fake_now_naive
        mock_dt.fromisoformat = datetime.datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
        return mock_dt

    return patch("agents.poster.datetime.datetime", wraps=datetime.datetime,
                 **{"now.return_value": _FAKE_NOW}), \
           patch("core.safety.datetime", _mock_safety_dt_factory())


class TestPosterExecute:
    """Tests for PosterAgent.execute."""

    def test_publishes_pending_post(self, poster_env) -> None:
        """A pending post whose scheduled_at is in the past should be published."""
        agent, sm, mock_api = poster_env

        sm.save_json("post_queue.json", {
            "last_updated": None,
            "queue": [{
                "id": "q_test_001",
                "research_id": "res_test_001",
                "content": "テスト投稿です",
                "hashtag": "#テスト",
                "pattern": "短文完結型",
                "quality_score": 8.0,
                "similarity_score": 0.2,
                "category": "skincare_knowledge",
                "scheduled_at": _FAKE_PAST,
                "created_at": _FAKE_PAST,
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": None,
            }],
        })

        p1, p2 = _patch_datetime_now()
        with p1, p2:
            agent.execute()

        # Threads API should have been called
        mock_api.create_text_post.assert_called_once_with("テスト投稿です")

        # Post history should contain the new post
        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 1
        assert history["posts"][0]["threads_media_id"] == "17890012345678901"

        # Queue item status should be "posted"
        queue = sm.load_json("post_queue.json")
        assert queue["queue"][0]["status"] == "posted"

    def test_skips_future_post(self, poster_env) -> None:
        """A post scheduled for the future should not be published."""
        agent, sm, mock_api = poster_env
        future = (_FAKE_NOW + datetime.timedelta(hours=2)).isoformat()

        sm.save_json("post_queue.json", {
            "last_updated": None,
            "queue": [{
                "id": "q_test_002",
                "research_id": "res_test_002",
                "content": "未来の投稿",
                "hashtag": "#テスト",
                "pattern": "短文完結型",
                "quality_score": 8.0,
                "similarity_score": 0.2,
                "category": "skincare_knowledge",
                "scheduled_at": future,
                "created_at": _FAKE_NOW.isoformat(),
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": None,
            }],
        })

        p1, p2 = _patch_datetime_now()
        with p1, p2:
            agent.execute()
        mock_api.create_text_post.assert_not_called()

    def test_skips_when_queue_empty(self, poster_env) -> None:
        """When the queue is empty, execute should return without error."""
        agent, sm, mock_api = poster_env
        sm.save_json("post_queue.json", {"last_updated": None, "queue": []})

        agent.execute()
        mock_api.create_text_post.assert_not_called()

    def test_blocked_by_emergency_stop(self, poster_env) -> None:
        """When emergency stop is active, posting should be blocked."""
        agent, sm, mock_api = poster_env

        sm.save_json("post_queue.json", {
            "last_updated": None,
            "queue": [{
                "id": "q_test_003",
                "content": "ブロックされるべき投稿",
                "scheduled_at": _FAKE_PAST,
                "status": "pending",
                "affiliate_comment": None,
            }],
        })

        # Activate emergency stop
        sm.save_json("system_state.json", {
            "emergency_stop": True,
            "emergency_stop_reason": "test",
            "emergency_stop_at": _FAKE_NOW.isoformat(),
            "agent_status": {},
            "daily_counters": {},
        })

        p1, p2 = _patch_datetime_now()
        with p1, p2:
            agent.execute()
        mock_api.create_text_post.assert_not_called()

    def test_affiliate_comment_gets_pr_label(self, poster_env) -> None:
        """Affiliate comments should always have a PR label prepended."""
        agent, sm, mock_api = poster_env

        sm.save_json("post_queue.json", {
            "last_updated": None,
            "queue": [{
                "id": "q_test_004",
                "research_id": "res_test_004",
                "content": "本文テスト",
                "hashtag": "#テスト",
                "pattern": "短文完結型",
                "quality_score": 8.0,
                "similarity_score": 0.2,
                "category": "skincare_knowledge",
                "scheduled_at": _FAKE_PAST,
                "created_at": _FAKE_PAST,
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": "おすすめの化粧水はこちら！",
            }],
        })

        p1, p2 = _patch_datetime_now()
        with p1, p2:
            agent.execute()

        # Should be called twice: main post + affiliate reply
        assert mock_api.create_text_post.call_count == 2

        # Second call (affiliate) should start with "PR"
        affiliate_call_args = mock_api.create_text_post.call_args_list[1]
        affiliate_text = affiliate_call_args[0][0] if affiliate_call_args[0] else affiliate_call_args[1].get("text", "")
        if not affiliate_text:
            affiliate_text = str(affiliate_call_args)
        assert "PR" in affiliate_text


class TestPosterThreadPublishing:
    """Tests for thread (multi-post) publishing."""

    def test_publishes_thread_posts_as_chain(self, poster_env) -> None:
        """Thread posts should be published as a chain of self-replies."""
        agent, sm, mock_api = poster_env

        # Track the IDs returned by successive create_text_post calls
        mock_api.create_text_post.side_effect = [
            {"id": "main_001"},     # main post
            {"id": "thread_002"},   # thread post 1
            {"id": "thread_003"},   # thread post 2
        ]

        sm.save_json("post_queue.json", {
            "last_updated": None,
            "queue": [{
                "id": "q_thread_001",
                "research_id": "res_001",
                "content": "1ポスト目: フック",
                "thread_posts": ["2ポスト目: 解説", "3ポスト目: まとめ"],
                "hashtag": "#テスト",
                "pattern": "ツリー展開型",
                "quality_score": 8.5,
                "similarity_score": 0.2,
                "category": "skincare_ingredients",
                "scheduled_at": _FAKE_PAST,
                "created_at": _FAKE_PAST,
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": None,
            }],
        })

        p1, p2 = _patch_datetime_now()
        with p1, p2, patch("agents.poster.time.sleep"):
            agent.execute()

        # Should have 3 API calls: main + 2 thread posts
        assert mock_api.create_text_post.call_count == 3

        # First call: main post (no reply_to_id)
        first_call = mock_api.create_text_post.call_args_list[0]
        assert first_call[0][0] == "1ポスト目: フック"

        # Second call: reply to main
        second_call = mock_api.create_text_post.call_args_list[1]
        assert second_call[0][0] == "2ポスト目: 解説"
        assert second_call[1]["reply_to_id"] == "main_001"

        # Third call: reply to second (chain)
        third_call = mock_api.create_text_post.call_args_list[2]
        assert third_call[0][0] == "3ポスト目: まとめ"
        assert third_call[1]["reply_to_id"] == "thread_002"

        # Post history should record thread_media_ids
        history = sm.load_json("post_history.json")
        assert history["posts"][0]["thread_media_ids"] == ["thread_002", "thread_003"]

    def test_single_post_has_no_thread_media_ids(self, poster_env) -> None:
        """Regular (non-thread) posts should have thread_media_ids=None."""
        agent, sm, mock_api = poster_env

        sm.save_json("post_queue.json", {
            "last_updated": None,
            "queue": [{
                "id": "q_single_001",
                "content": "通常の単一投稿",
                "scheduled_at": _FAKE_PAST,
                "status": "pending",
                "affiliate_comment": None,
            }],
        })

        p1, p2 = _patch_datetime_now()
        with p1, p2:
            agent.execute()

        assert mock_api.create_text_post.call_count == 1
        history = sm.load_json("post_history.json")
        assert history["posts"][0]["thread_media_ids"] is None

    def test_thread_with_empty_list_treated_as_single(self, poster_env) -> None:
        """thread_posts=[] should behave as a single post."""
        agent, sm, mock_api = poster_env

        sm.save_json("post_queue.json", {
            "last_updated": None,
            "queue": [{
                "id": "q_empty_thread",
                "content": "空リストのスレッド",
                "thread_posts": [],
                "scheduled_at": _FAKE_PAST,
                "status": "pending",
                "affiliate_comment": None,
            }],
        })

        p1, p2 = _patch_datetime_now()
        with p1, p2:
            agent.execute()

        assert mock_api.create_text_post.call_count == 1


class TestPosterPrLabel:
    """Edge cases for the PR label enforcement logic."""

    def test_does_not_double_label(self) -> None:
        """If PR is already present, it should not be added again."""
        result = PosterAgent._ensure_pr_label("PR\nこの商品おすすめ！")
        assert not result.startswith("PR\nPR")

    def test_fullwidth_pr(self) -> None:
        """Full-width ＰＲ should be recognised."""
        result = PosterAgent._ensure_pr_label("【ＰＲ】おすすめ！")
        assert result == "【ＰＲ】おすすめ！"
