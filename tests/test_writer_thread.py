"""Tests for writer thread generation and pattern ratio control."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager
from core.safety import SafetyGuard

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime.datetime(2026, 3, 28, 14, 0, 0, tzinfo=_JST)


@pytest.fixture()
def writer_env(tmp_path: Path):
    """Set up a WriterAgent with mocked services."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sm = StateManager(state_dir=state_dir)

    sm.save_json("system_state.json", {
        "emergency_stop": False,
        "agent_status": {},
        "daily_counters": {},
    })
    sm.save_json("post_history.json", {"posts": [], "daily_stats": {}})
    sm.save_json("post_queue.json", {"queue": []})
    sm.save_json("research_pool.json", {"items": []})

    env_vars = {
        "THREADS_ACCESS_TOKEN": "test_token",
        "THREADS_USER_ID": "test_user",
        "ANTHROPIC_API_KEY": "test_key",
    }

    with patch.dict("os.environ", env_vars):
        with patch("agents.writer.ClaudeClient") as mock_claude_cls:
            mock_claude = MagicMock()
            mock_claude_cls.return_value = mock_claude
            from agents.writer import WriterAgent
            agent = WriterAgent()
            agent.state = sm
            agent.safety = SafetyGuard(sm)
            yield agent, sm, mock_claude


class TestThreadRatio:
    """Tests for _get_current_thread_ratio."""

    def test_zero_when_no_posts(self, writer_env) -> None:
        agent, sm, _ = writer_env
        assert agent._get_current_thread_ratio() == 0.0

    def test_computes_ratio_from_history(self, writer_env) -> None:
        agent, sm, _ = writer_env
        posts = []
        for i in range(10):
            pattern = "ツリー展開型" if i < 4 else "短文完結型"
            posts.append({
                "id": f"post_{i}",
                "pattern": pattern,
                "content": f"テスト投稿{i}",
                "posted_at": _NOW.isoformat(),
            })
        sm.save_json("post_history.json", {"posts": posts})
        ratio = agent._get_current_thread_ratio()
        assert ratio == pytest.approx(0.4, abs=0.01)

    def test_uses_only_last_20_posts(self, writer_env) -> None:
        """When > 20 posts exist, only the last 20 should be used for ratio."""
        agent, sm, _ = writer_env
        # 50 posts: first 30 are thread, last 20 are non-thread
        posts = []
        for i in range(50):
            pattern = "ツリー展開型" if i < 30 else "短文完結型"
            posts.append({
                "id": f"post_{i}",
                "pattern": pattern,
                "content": f"テスト投稿{i}",
                "posted_at": _NOW.isoformat(),
            })
        sm.save_json("post_history.json", {"posts": posts})
        # Only last 20 (all 短文完結型) should be considered → ratio = 0.0
        ratio = agent._get_current_thread_ratio()
        assert ratio == pytest.approx(0.0, abs=0.01)

    def test_includes_pending_queue(self, writer_env) -> None:
        agent, sm, _ = writer_env
        sm.save_json("post_history.json", {"posts": []})
        sm.save_json("post_queue.json", {
            "queue": [
                {"id": "q1", "pattern": "ツリー展開型", "status": "pending"},
                {"id": "q2", "pattern": "短文完結型", "status": "pending"},
            ],
        })
        ratio = agent._get_current_thread_ratio()
        assert ratio == pytest.approx(0.5, abs=0.01)


class TestParseThreadResponse:
    """Tests for _parse_thread_response."""

    def test_parses_separator_format(self, writer_env) -> None:
        agent, *_ = writer_env
        raw = "1ポスト目のフック\n---THREAD_BREAK---\n2ポスト目の解説"
        parts = agent._parse_thread_response(raw, 2)
        assert parts is not None
        assert len(parts) == 2
        assert "1ポスト目" in parts[0]
        assert "2ポスト目" in parts[1]

    def test_parses_three_posts(self, writer_env) -> None:
        agent, *_ = writer_env
        raw = "フック\n---THREAD_BREAK---\n解説\n---THREAD_BREAK---\nまとめ"
        parts = agent._parse_thread_response(raw, 3)
        assert parts is not None
        assert len(parts) == 3

    def test_caps_at_expected_count(self, writer_env) -> None:
        agent, *_ = writer_env
        raw = "A\n---THREAD_BREAK---\nB\n---THREAD_BREAK---\nC\n---THREAD_BREAK---\nD"
        parts = agent._parse_thread_response(raw, 2)
        assert parts is not None
        assert len(parts) == 2

    def test_returns_none_for_single_block(self, writer_env) -> None:
        agent, *_ = writer_env
        raw = "分割されていない単一テキスト"
        parts = agent._parse_thread_response(raw, 2)
        assert parts is None

    def test_fallback_triple_dash_separator(self, writer_env) -> None:
        agent, *_ = writer_env
        raw = "フック部分\n---\n解説部分"
        parts = agent._parse_thread_response(raw, 2)
        assert parts is not None
        assert len(parts) == 2


class TestBuildThreadPrompt:
    """Tests for _build_thread_prompt."""

    def test_contains_thread_separator_instruction(self, writer_env) -> None:
        agent, *_ = writer_env
        research = {"topic": "セラミド", "summary": "保湿成分", "category": "skincare_ingredients"}
        prompt = agent._build_thread_prompt(research, 3)
        assert "THREAD_BREAK" in prompt
        assert "3つのポスト" in prompt

    def test_includes_debate_when_provided(self, writer_env) -> None:
        agent, *_ = writer_env
        research = {"topic": "テスト", "summary": "テスト", "category": "test"}
        debate = {
            "title": "無添加は本当に安全か？",
            "stance": "成分表を読む習慣が大事",
            "talking_points": ["法的定義がない"],
            "ng_statements": ["無添加を選ぶ人はバカ"],
        }
        prompt = agent._build_thread_prompt(research, 2, debate=debate)
        assert "無添加は本当に安全か？" in prompt
        assert "禁止表現" in prompt
