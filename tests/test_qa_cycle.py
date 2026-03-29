"""Tests for QA solicitation/answer cycle (Writer + Replier)."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime.datetime(2026, 3, 27, 14, 0, 0, tzinfo=_JST)  # Friday
_TODAY_KEY = _NOW.strftime("%Y-%m-%d")
_TUESDAY = datetime.datetime(2026, 3, 24, 14, 0, 0, tzinfo=_JST)


# ------------------------------------------------------------------
# Replier: question detection
# ------------------------------------------------------------------


def _make_candidate(text: str, username: str = "user1") -> dict:
    return {
        "comment_id": f"c_{hash(text) % 10000:04d}",
        "comment_text": text,
        "comment_username": username,
        "comment_timestamp": _NOW.isoformat(),
        "post_id": "post_001",
        "post_media_id": "media_001",
        "post_content": "テスト投稿",
    }


class TestQuestionDetection:

    @pytest.fixture()
    def replier_env(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sm = StateManager(state_dir=state_dir)

        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "emergency_stop_reason": None,
            "emergency_stop_at": None,
            "agent_status": {},
            "daily_counters": {},
        })
        sm.save_json("post_history.json", {"posts": [], "daily_stats": {}})
        sm.save_json("reply_state.json", {
            "processed_comments": {},
            "daily_reply_count": {},
        })
        sm.save_json("research_pool.json", {"items": [], "last_updated": None})

        env_vars = {
            "THREADS_ACCESS_TOKEN": "test_token",
            "THREADS_USER_ID": "test_user",
        }

        with patch.dict("os.environ", env_vars):
            with patch("agents.replier.ThreadsAPIClient") as mock_api_cls:
                with patch("agents.replier.ClaudeClient") as mock_claude_cls:
                    with patch("agents.replier.QualityGate") as mock_qg_cls:
                        mock_api = MagicMock()
                        mock_api.user_id = "test_user"
                        mock_api_cls.return_value = mock_api

                        mock_claude = MagicMock()
                        mock_claude_cls.return_value = mock_claude

                        mock_qg = MagicMock()
                        mock_qg.check_ng_words.return_value = None
                        mock_qg_cls.return_value = mock_qg

                        from agents.replier import ReplierAgent

                        agent = ReplierAgent()
                        agent.state = sm
                        yield agent, sm

    def test_detects_question_with_question_mark(self, replier_env) -> None:
        agent, sm = replier_env
        candidates = [
            _make_candidate("セラミド配合の化粧水、おすすめある？教えて！"),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._detect_and_pool_questions(candidates, {})

        pool = sm.load_json("research_pool.json")
        items = pool.get("items", [])
        assert len(items) == 1
        assert items[0]["source"] == "follower_question"
        assert "セラミド" in items[0]["question_text"]

    def test_ignores_short_comments(self, replier_env) -> None:
        agent, sm = replier_env
        candidates = [
            _make_candidate("何？"),  # Too short (< 10 chars)
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._detect_and_pool_questions(candidates, {})

        pool = sm.load_json("research_pool.json")
        assert len(pool.get("items", [])) == 0

    def test_ignores_non_questions(self, replier_env) -> None:
        agent, sm = replier_env
        candidates = [
            _make_candidate("すごく参考になりました！ありがとう！"),
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._detect_and_pool_questions(candidates, {})

        pool = sm.load_json("research_pool.json")
        assert len(pool.get("items", [])) == 0

    def test_deduplicates_existing_questions(self, replier_env) -> None:
        agent, sm = replier_env

        # Pre-populate with existing question
        sm.save_json("research_pool.json", {
            "items": [{
                "id": "fq_existing",
                "source": "follower_question",
                "question_text": "化粧水のおすすめ教えて？",
                "used": False,
            }],
        })

        candidates = [
            _make_candidate("化粧水のおすすめ教えて？"),  # Exact duplicate
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._detect_and_pool_questions(candidates, {})

        pool = sm.load_json("research_pool.json")
        assert len(pool["items"]) == 1  # No new item added

    def test_limits_to_5_per_run(self, replier_env) -> None:
        agent, sm = replier_env
        candidates = [
            _make_candidate(f"質問{i}: スキンケアについて教えて？", f"user_{i}")
            for i in range(10)
        ]

        with patch("agents.replier.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._detect_and_pool_questions(candidates, {})

        pool = sm.load_json("research_pool.json")
        assert len(pool["items"]) <= 5


# ------------------------------------------------------------------
# Writer: QA solicitation scheduling
# ------------------------------------------------------------------


class TestQASolicitation:

    @pytest.fixture()
    def writer_env(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sm = StateManager(state_dir=state_dir)

        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "emergency_stop_reason": None,
            "emergency_stop_at": None,
            "agent_status": {},
            "daily_counters": {},
        })
        sm.save_json("post_queue.json", {"queue": [], "last_updated": None})
        sm.save_json("post_history.json", {"posts": [], "daily_stats": {}})
        sm.save_json("research_pool.json", {"items": [], "last_updated": None})

        env_vars = {
            "THREADS_ACCESS_TOKEN": "test_token",
            "THREADS_USER_ID": "test_user",
        }

        with patch.dict("os.environ", env_vars):
            with patch("agents.writer.ClaudeClient") as mock_claude_cls:
                with patch("agents.writer.QualityGate") as mock_qg_cls:
                    mock_claude = MagicMock()
                    mock_claude.generate_post.return_value = (
                        "最近よく聞かれるスキンケアの質問、\n"
                        "まとめて答えようと思ってるよ。\n\n"
                        "気になることコメントで教えて！"
                    )
                    mock_claude_cls.return_value = mock_claude

                    mock_qg = MagicMock()
                    mock_qg.check_ng_words.return_value = None
                    mock_qg.validate.return_value = {
                        "passed": True,
                        "similarity_score": 0.1,
                    }
                    mock_qg_cls.return_value = mock_qg

                    from agents.writer import WriterAgent

                    agent = WriterAgent()
                    agent.state = sm
                    yield agent, sm

    def test_should_generate_on_schedule_day(self, writer_env) -> None:
        agent, sm = writer_env
        # Friday is in the default schedule
        with patch("agents.writer_content.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW  # Friday
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = agent._should_generate_qa_solicitation()
        assert result is True

    def test_should_not_generate_on_non_schedule_day(self, writer_env) -> None:
        agent, sm = writer_env
        # Wednesday is not in the default schedule
        wednesday = datetime.datetime(2026, 3, 25, 14, 0, 0, tzinfo=_JST)
        with patch("agents.writer_content.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = wednesday
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = agent._should_generate_qa_solicitation()
        assert result is False

    def test_should_not_generate_if_already_queued(self, writer_env) -> None:
        agent, sm = writer_env
        sm.save_json("post_queue.json", {
            "queue": [{
                "id": "q_existing",
                "pattern": "質問募集型",
                "created_at": _NOW.isoformat(),
                "status": "pending",
            }],
        })

        with patch("agents.writer_content.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = agent._should_generate_qa_solicitation()
        assert result is False

    def test_generates_qa_solicitation_post(self, writer_env) -> None:
        agent, sm = writer_env

        # Mock _select_time_slot to avoid datetime comparison issues
        with patch.object(agent, "_select_time_slot", return_value=_NOW.isoformat()):
            post = agent._generate_qa_solicitation_post(_NOW)

        assert post is not None
        assert post["pattern"] == "質問募集型"
        assert post["research_id"] == "qa_solicitation"
        assert post["status"] == "pending"


# ------------------------------------------------------------------
# Writer: QA answer pattern selection
# ------------------------------------------------------------------


class TestQAPatternSelection:

    def test_all_patterns_includes_qa_types(self) -> None:
        from agents.writer_constants import _ALL_PATTERNS

        assert "質問募集型" in _ALL_PATTERNS
        assert "フォロワー質問回答型" in _ALL_PATTERNS
