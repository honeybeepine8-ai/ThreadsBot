"""Tests for hook A/B test in WriterAgent and hook collection in FetcherAgent."""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime.datetime(2026, 3, 28, 14, 0, 0, tzinfo=_JST)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------
# WriterAgent hook helpers
# ------------------------------------------------------------------


class TestLoadHookStock:

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        with patch("agents.writer_constants._PROJECT_ROOT", tmp_path):
            from agents.writer import WriterAgent

            hooks = WriterAgent._load_hook_stock()
        assert hooks == []

    def test_loads_and_sorts_by_engagement(self, tmp_path: Path) -> None:
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        stock = {
            "last_updated": "2026-03-28T10:00:00+09:00",
            "hooks": [
                {"id": "h1", "text": "hook1", "engagement_rate": 0.03},
                {"id": "h2", "text": "hook2", "engagement_rate": 0.10},
                {"id": "h3", "text": "hook3", "engagement_rate": 0.05},
            ],
        }
        (knowledge_dir / "hook_stock.json").write_text(
            json.dumps(stock, ensure_ascii=False), encoding="utf-8"
        )

        with patch("agents.writer_constants._PROJECT_ROOT", tmp_path):
            from agents.writer import WriterAgent

            hooks = WriterAgent._load_hook_stock()

        assert len(hooks) == 3
        assert hooks[0]["id"] == "h2"  # Highest engagement first


# ------------------------------------------------------------------
# FetcherAgent._collect_buzz_hooks
# ------------------------------------------------------------------


def _make_buzz_post(
    post_id: str,
    content: str,
    views: int = 1000,
    likes: int = 80,
    category: str = "skincare_knowledge",
    pattern: str = "短文完結型",
) -> dict:
    return {
        "id": post_id,
        "content": content,
        "category": category,
        "pattern": pattern,
        "metrics": {
            "views": views,
            "likes": likes,
            "replies": 5,
            "reposts": 3,
            "quotes": 2,
            "last_fetched": _NOW.isoformat(),
        },
    }


class TestCollectBuzzHooks:

    @pytest.fixture()
    def fetcher_env(self, tmp_path: Path):
        """Create a FetcherAgent with patches kept active for the test."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        real_config = _PROJECT_ROOT / "config" / "settings.yaml"
        shutil.copy2(real_config, config_dir / "settings.yaml")
        aff_products = _PROJECT_ROOT / "config" / "affiliate_products.yaml"
        if aff_products.exists():
            shutil.copy2(aff_products, config_dir / "affiliate_products.yaml")

        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "hook_stock.json").write_text(
            '{"last_updated": null, "hooks": []}', encoding="utf-8"
        )

        analytics_dir = tmp_path / "data" / "analytics"
        analytics_dir.mkdir(parents=True)

        sm = StateManager(state_dir=state_dir)
        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "emergency_stop_reason": None,
            "emergency_stop_at": None,
            "agent_status": {},
            "daily_counters": {},
        })
        sm.save_json("post_history.json", {"posts": [], "daily_stats": {}})

        env_vars = {
            "THREADS_ACCESS_TOKEN": "test_token",
            "THREADS_USER_ID": "test_user",
        }

        with patch.dict("os.environ", env_vars), \
             patch("agents.fetcher._PROJECT_ROOT", tmp_path), \
             patch("agents.fetcher._ANALYTICS_DIR", analytics_dir), \
             patch("agents.fetcher.ThreadsAPIClient") as mock_api_cls, \
             patch("agents.fetcher.ClaudeClient") as mock_claude_cls:
            mock_api_cls.return_value = MagicMock()
            mock_claude_cls.return_value = MagicMock()

            from agents.fetcher import FetcherAgent

            agent = FetcherAgent()
            agent.state = sm
            yield agent, sm, tmp_path

    def test_collects_buzz_hooks(self, fetcher_env) -> None:
        agent, sm, root = fetcher_env

        history = {
            "posts": [
                _make_buzz_post(
                    "post_001",
                    "今使ってる化粧水、裏返してみて。\n\n成分表に...",
                ),
            ]
        }

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._collect_buzz_hooks(history)

        stock_path = root / "knowledge" / "hook_stock.json"
        with open(stock_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data["hooks"]) == 1
        assert data["hooks"][0]["text"] == "今使ってる化粧水、裏返してみて。"
        assert data["hooks"][0]["source_post_id"] == "post_001"

    def test_skips_below_threshold(self, fetcher_env) -> None:
        agent, sm, root = fetcher_env

        history = {
            "posts": [
                _make_buzz_post("post_low", "低PV投稿です。", views=100, likes=2),
            ]
        }

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._collect_buzz_hooks(history)

        stock_path = root / "knowledge" / "hook_stock.json"
        with open(stock_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data["hooks"]) == 0

    def test_skips_already_collected(self, fetcher_env) -> None:
        agent, sm, root = fetcher_env

        stock_path = root / "knowledge" / "hook_stock.json"
        pre_data = {
            "last_updated": _NOW.isoformat(),
            "hooks": [
                {"id": "h_001", "text": "既存フック", "source_post_id": "post_001"},
            ],
        }
        stock_path.write_text(
            json.dumps(pre_data, ensure_ascii=False), encoding="utf-8"
        )

        history = {
            "posts": [
                _make_buzz_post("post_001", "既存フック\nこの投稿は既に収集済み"),
            ]
        }

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._collect_buzz_hooks(history)

        with open(stock_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data["hooks"]) == 1

    def test_caps_at_200_hooks(self, fetcher_env) -> None:
        agent, sm, root = fetcher_env

        stock_path = root / "knowledge" / "hook_stock.json"
        pre_hooks = [
            {
                "id": f"h_{i:03d}",
                "text": f"既存フック{i}",
                "source_post_id": f"old_{i}",
                "engagement_rate": 0.01,
            }
            for i in range(199)
        ]
        stock_path.write_text(
            json.dumps({"last_updated": None, "hooks": pre_hooks}, ensure_ascii=False),
            encoding="utf-8",
        )

        posts = [
            _make_buzz_post(f"new_{i}", f"新しいフック{i}\n本文", views=1000, likes=100)
            for i in range(5)
        ]
        history = {"posts": posts}

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent._collect_buzz_hooks(history)

        with open(stock_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data["hooks"]) <= 200
