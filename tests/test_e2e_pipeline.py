"""E2E integration tests — generation-to-posting pipeline (P4-1).

Tests the full flow: research_pool → Writer → draft_queue/post_queue → Poster → post_history
All external APIs (Claude, Threads) are mocked.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager

_JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture()
def sm(tmp_path: Path) -> StateManager:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return StateManager(state_dir=state_dir)


def _seed_research(sm: StateManager) -> None:
    """Seed research_pool with one unused item."""
    sm.save_json("research_pool.json", {
        "last_updated": None,
        "items": [
            {
                "id": "r_test_001",
                "topic": "ビタミンCの効果",
                "summary": "ビタミンC誘導体はメラニン生成を抑制し、美白効果が期待できる。",
                "category": "skincare_ingredients",
                "priority": 8,
                "used": False,
                "source": "test",
            }
        ],
    })


def _seed_system(sm: StateManager, *, emergency: bool = False, posts_today: int = 0) -> None:
    """Seed system_state."""
    now = datetime.datetime.now(_JST)
    today = now.strftime("%Y-%m-%d")
    sm.save_json("system_state.json", {
        "emergency_stop": emergency,
        "agent_status": {},
        "daily_counters": {
            today: {"posts": posts_today, "posts_published": posts_today}
        },
    })


def _seed_empty_queues(sm: StateManager) -> None:
    sm.save_json("post_queue.json", {"queue": []})
    sm.save_json("draft_queue.json", {"drafts": [], "stats": {
        "total_generated": 0, "auto_approved": 0,
        "manually_approved": 0, "rejected": 0, "expired": 0,
    }})
    sm.save_json("post_history.json", {"posts": [], "daily_stats": {}})


def _mock_claude_high_quality():
    """Mock Claude to return high-quality content + evaluation."""
    mock = MagicMock()

    # generate_post: returns post content
    mock.generate_post.return_value = (
        "ビタミンC誘導体って聞いたことある？\n\n"
        "実はメラニン生成を抑える成分で\n"
        "美白ケアの王道なんだよね\n\n"
        "朝のスキンケアに取り入れるのがおすすめ"
    )

    # evaluate_quality: returns high score
    mock.evaluate_quality.return_value = {
        "scores": {
            "usefulness": 9.0,
            "specificity": 8.5,
            "empathy": 8.0,
            "naturalness": 9.0,
            "tempo": 8.5,
            "experiential": 8.0,
            "non_commercial": 9.5,
        },
        "content_quality_avg": 8.5,
        "expression_quality_avg": 8.75,
        "average": 8.6,
        "feedback": "Good post.",
    }

    return mock


def _mock_claude_low_quality():
    """Mock Claude to return low-quality content."""
    mock = MagicMock()
    mock.generate_post.return_value = "肌にいい"
    mock.evaluate_quality.return_value = {
        "scores": {},
        "content_quality_avg": 4.0,
        "expression_quality_avg": 4.0,
        "average": 4.0,
        "feedback": "Too short.",
    }
    return mock


def _mock_threads_api():
    """Mock Threads API to return success."""
    mock = MagicMock()
    mock.create_text_post.return_value = {"id": "threads_media_12345"}
    mock.get_post_insights.return_value = {
        "views": 100, "likes": 10, "replies": 2, "reposts": 1, "quotes": 0,
    }
    return mock


class TestWriterExecuteE2E:
    """Writer.execute() actually generates posts from research pool."""

    def test_writer_generates_and_enqueues_from_research(self, sm: StateManager):
        """Full Writer.execute(): research → generate → quality gate → enqueue.

        Pattern is pinned to 短文完結型 to avoid thread/QA paths that
        require different mock setups.
        """
        _seed_research(sm)
        _seed_system(sm)
        _seed_empty_queues(sm)

        mock_claude = _mock_claude_high_quality()
        # generate_post returns content that passes quality + length checks
        mock_claude.generate_post.return_value = (
            "ビタミンC誘導体って聞いたことある？\n\n"
            "メラニン生成を抑えて #美容成分 の王道なんだよね\n\n"
            "朝のスキンケアに取り入れるのがおすすめ"
        )
        # fact_checker mock — must match FactChecker.run_all_checks() schema
        mock_fact_checker = MagicMock()
        mock_fact_checker.run_all_checks.return_value = {
            "blocked": False,
            "block_reason": "",
            "pharma_law": {"result": "safe", "reason": ""},
            "numerical": {
                "fact_check_required": False,
                "claims_found": [],
                "unknown_claims": [],
            },
            "fact_check_required": False,
            "review_required": False,
        }

        env_vars = {
            "THREADS_ACCESS_TOKEN": "test_token",
            "THREADS_USER_ID": "test_user",
            "ANTHROPIC_API_KEY": "test_key",
        }

        with patch.dict("os.environ", env_vars):
            with patch("agents.writer.ClaudeClient", return_value=mock_claude):
                from agents.writer import WriterAgent
                writer = WriterAgent()
                writer.state = sm
                from core.safety import SafetyGuard
                writer.safety = SafetyGuard(sm)
                writer.fact_checker = mock_fact_checker

                # Pin pattern to single-post; disable UGC/QA special posts
                with patch.object(writer, "_select_pattern", return_value="短文完結型"), \
                     patch.object(writer, "_should_generate_ugc", return_value=False), \
                     patch.object(writer, "_should_generate_qa_solicitation", return_value=False):
                    writer.execute()

        # Claude was called exactly once each for generation and evaluation
        assert mock_claude.generate_post.call_count == 1
        assert mock_claude.evaluate_quality.call_count == 1

        # Score 8.6 < auto_approve_threshold (10.1) → draft_queue (pending_review)
        queue = sm.load_json("post_queue.json")
        assert len(queue["queue"]) == 0  # not auto-approved; stays in draft

        # draft_queue should have one pending_review record
        drafts = sm.load_json("draft_queue.json")
        assert len(drafts["drafts"]) == 1
        draft = drafts["drafts"][0]
        assert draft["status"] == "pending_review"
        assert draft["pattern"] == "短文完結型"
        assert draft["quality_score"] == 8.6
        assert "ビタミンC" in draft["content"]
        assert draft["flags"]["auto_approved"] is False

        # Research item should be marked as used
        pool = sm.load_json("research_pool.json")
        assert pool["items"][0]["used"] is True


class TestHappyPath:
    """Poster publishes from queue to post_history."""

    def test_poster_publishes_pending_post(self, sm: StateManager):
        _seed_system(sm)
        _seed_empty_queues(sm)

        now = datetime.datetime.now(_JST)
        queue = sm.load_json("post_queue.json")
        queue["queue"].append({
            "id": f"q_{now.strftime('%Y%m%d')}_001",
            "content": "ビタミンC誘導体って聞いたことある？\n\n美白ケアの王道なんだよね",
            "hashtag": "#スキンケア",
            "pattern": "短文完結型",
            "quality_score": 8.6,
            "category": "skincare_ingredients",
            "scheduled_at": (now - datetime.timedelta(minutes=5)).isoformat(),
            "status": "pending",
        })
        sm.save_json("post_queue.json", queue)

        from agents.poster import PosterAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(PosterAgent, "__init__", lambda self: None):
            poster = PosterAgent.__new__(PosterAgent)
            poster.name = "poster"
            poster.state = sm
            poster.logger = get_logger("poster")
            poster.safety = SafetyGuard(sm)
            poster.threads = _mock_threads_api()
            poster.notifier = Notifier()

        poster.execute()

        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 1
        posted = history["posts"][0]
        assert posted["threads_media_id"] == "threads_media_12345"
        assert "ビタミンC" in posted["content"]

        queue = sm.load_json("post_queue.json")
        assert queue["queue"][0]["status"] == "posted"


class TestSafetyBlock:
    """Safety guard blocks posting."""

    def test_daily_limit_blocks_poster(self, sm: StateManager):
        _seed_system(sm, posts_today=6)
        _seed_empty_queues(sm)

        now = datetime.datetime.now(_JST)
        queue = sm.load_json("post_queue.json")
        queue["queue"].append({
            "id": "q_test_001",
            "content": "test post",
            "scheduled_at": (now - datetime.timedelta(minutes=1)).isoformat(),
            "status": "pending",
        })
        sm.save_json("post_queue.json", queue)

        from agents.poster import PosterAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(PosterAgent, "__init__", lambda self: None):
            poster = PosterAgent.__new__(PosterAgent)
            poster.name = "poster"
            poster.state = sm
            poster.logger = get_logger("poster")
            poster.safety = SafetyGuard(sm)
            poster.threads = _mock_threads_api()
            poster.notifier = Notifier()

        poster.execute()

        # Post should NOT be published
        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 0

    def test_emergency_stop_blocks_poster(self, sm: StateManager):
        _seed_system(sm, emergency=True)
        _seed_empty_queues(sm)

        # Add a pending item to the queue
        now = datetime.datetime.now(_JST)
        queue = sm.load_json("post_queue.json")
        queue["queue"].append({
            "id": "q_test_emg",
            "content": "should not be posted",
            "scheduled_at": (now - datetime.timedelta(minutes=1)).isoformat(),
            "status": "pending",
        })
        sm.save_json("post_queue.json", queue)

        from agents.poster import PosterAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(PosterAgent, "__init__", lambda self: None):
            poster = PosterAgent.__new__(PosterAgent)
            poster.name = "poster"
            poster.state = sm
            poster.logger = get_logger("poster")
            poster.safety = SafetyGuard(sm)
            poster.threads = _mock_threads_api()
            poster.notifier = Notifier()

        # BaseAgent.run() checks emergency_stop, but execute() does not.
        # The safety.can_post() check inside execute() catches it.
        poster.execute()

        # Post should NOT be published
        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 0


class TestNoPendingPosts:
    """Poster handles empty queue gracefully."""

    def test_no_pending_posts(self, sm: StateManager):
        _seed_system(sm)
        _seed_empty_queues(sm)

        from agents.poster import PosterAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(PosterAgent, "__init__", lambda self: None):
            poster = PosterAgent.__new__(PosterAgent)
            poster.name = "poster"
            poster.state = sm
            poster.logger = get_logger("poster")
            poster.safety = SafetyGuard(sm)
            poster.threads = _mock_threads_api()
            poster.notifier = Notifier()

        poster.execute()

        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 0


class TestThreadPosts:
    """Poster handles thread-format posts."""

    def test_thread_posts_chained(self, sm: StateManager):
        _seed_system(sm)
        _seed_empty_queues(sm)

        now = datetime.datetime.now(_JST)
        queue = sm.load_json("post_queue.json")
        queue["queue"].append({
            "id": "q_thread_001",
            "content": "スレッドの1つ目",
            "thread_posts": ["スレッドの2つ目", "スレッドの3つ目"],
            "scheduled_at": (now - datetime.timedelta(minutes=1)).isoformat(),
            "status": "pending",
            "pattern": "スレッド型",
            "category": "skincare_knowledge",
            "quality_score": 8.5,
        })
        sm.save_json("post_queue.json", queue)

        mock_threads = MagicMock()
        call_count = 0

        def fake_create_text_post(content, reply_to_id=None):
            nonlocal call_count
            call_count += 1
            return {"id": f"media_{call_count}"}

        mock_threads.create_text_post = fake_create_text_post

        from agents.poster import PosterAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(PosterAgent, "__init__", lambda self: None), \
             patch("agents.poster._THREAD_POST_DELAY_SECONDS", 0):
            poster = PosterAgent.__new__(PosterAgent)
            poster.name = "poster"
            poster.state = sm
            poster.logger = get_logger("poster")
            poster.safety = SafetyGuard(sm)
            poster.threads = mock_threads
            poster.notifier = Notifier()
            poster.execute()

        # 3 API calls: 1 main + 2 thread replies
        assert call_count == 3

        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 1
        posted = history["posts"][0]
        assert posted["threads_media_id"] == "media_1"
        assert posted["thread_media_ids"] == ["media_2", "media_3"]
