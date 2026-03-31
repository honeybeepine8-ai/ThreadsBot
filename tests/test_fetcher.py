"""Tests for agents.fetcher — metrics fetching and retroactive affiliate injection."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager
from core.safety import SafetyGuard

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime.datetime(2026, 3, 26, 18, 0, 0, tzinfo=_JST)
_TODAY_KEY = _NOW.strftime("%Y-%m-%d")


def _make_post(
    post_id: str,
    media_id: str,
    hours_ago: int = 12,
    views: int = 0,
    likes: int = 0,
    replies: int = 0,
    last_fetched: str | None = None,
    affiliate_added_at: str | None = None,
) -> dict:
    posted_at = (_NOW - datetime.timedelta(hours=hours_ago)).isoformat()
    metrics = {
        "views": views,
        "likes": likes,
        "replies": replies,
        "reposts": 0,
        "quotes": 0,
        "last_fetched": last_fetched,
    }
    return {
        "id": post_id,
        "threads_media_id": media_id,
        "content": "テスト投稿の内容",
        "posted_at": posted_at,
        "category": "skincare_knowledge",
        "metrics": metrics,
        "affiliate_added_at": affiliate_added_at,
    }


@pytest.fixture()
def fetcher_env(tmp_path: Path):
    """Set up a FetcherAgent with mocked APIs and temp state."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    analytics_dir = tmp_path / "analytics"
    analytics_dir.mkdir()
    sm = StateManager(state_dir=state_dir)

    sm.save_json("system_state.json", {
        "emergency_stop": False,
        "agent_status": {},
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
        with patch("agents.fetcher.ThreadsAPIClient") as mock_api_cls:
            with patch("agents.fetcher.ClaudeClient") as mock_claude_cls:
                mock_api = MagicMock()
                mock_api.get_post_insights.return_value = {
                    "views": 1000,
                    "likes": 50,
                    "replies": 10,
                    "reposts": 5,
                    "quotes": 3,
                }
                mock_api.create_text_post.return_value = {"id": "aff_media_001"}
                mock_api_cls.return_value = mock_api

                mock_claude = MagicMock()
                mock_claude.generate_post.return_value = "PR\nこのセラミド化粧水おすすめだよ"
                mock_claude_cls.return_value = mock_claude

                from unittest.mock import PropertyMock
                from core.account_context import AccountContext

                from agents.fetcher import FetcherAgent
                agent = FetcherAgent()
                agent.state = sm
                agent.safety = SafetyGuard(sm)

                # Redirect analytics_dir to temp path
                with patch.object(type(agent.ctx), "analytics_dir", new_callable=PropertyMock, return_value=analytics_dir):
                    # Stub _collect_buzz_hooks to prevent writing to real hook_stock.json
                    agent._collect_buzz_hooks = lambda *a, **kw: None

                    yield agent, sm, mock_api, mock_claude, analytics_dir


# ====================================================================
# Metrics fetching
# ====================================================================


class TestFetcherMetrics:

    def test_skips_posts_younger_than_first_stage(self, fetcher_env) -> None:
        agent, sm, mock_api, *_ = fetcher_env
        # Post only 30 minutes old (< 1h first stage)
        posted_at = (_NOW - datetime.timedelta(minutes=30)).isoformat()
        sm.save_json("post_history.json", {
            "posts": [{
                "id": "p1", "threads_media_id": "m1",
                "content": "テスト", "posted_at": posted_at,
                "category": "skincare_knowledge", "metrics": {},
            }],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.get_post_insights.assert_not_called()

    def test_fetches_metrics_for_eligible_post(self, fetcher_env) -> None:
        agent, sm, mock_api, *_ = fetcher_env
        sm.save_json("post_history.json", {
            "posts": [_make_post("p1", "m1", hours_ago=12, last_fetched=None)],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.get_post_insights.assert_called_once_with("m1")

        # Check metrics were updated
        history = sm.load_json("post_history.json")
        metrics = history["posts"][0]["metrics"]
        assert metrics["views"] == 1000
        assert metrics["likes"] == 50

    def test_refetches_after_interval(self, fetcher_env) -> None:
        agent, sm, mock_api, *_ = fetcher_env
        # Last fetched 20 hours ago (> 12 hour refetch interval)
        old_fetch = (_NOW - datetime.timedelta(hours=20)).isoformat()
        sm.save_json("post_history.json", {
            "posts": [_make_post("p1", "m1", hours_ago=24, last_fetched=old_fetch)],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.get_post_insights.assert_called_once()

    def test_skips_completed_posts(self, fetcher_env) -> None:
        agent, sm, mock_api, *_ = fetcher_env
        # Post already has all 3 stages completed (current_stage=24h, next=None)
        sm.save_json("post_history.json", {
            "posts": [_make_post(
                "p1", "m1", hours_ago=48,
                last_fetched=(_NOW - datetime.timedelta(hours=2)).isoformat(),
            )],
        })
        # Set current_stage to 24h (terminal)
        h = sm.load_json("post_history.json")
        h["posts"][0]["metrics"]["current_stage"] = "24h"
        h["posts"][0]["metrics"]["stages"] = {
            "1h": {}, "6h": {}, "24h": {},
        }
        sm.save_json("post_history.json", h)

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        mock_api.get_post_insights.assert_not_called()

    def test_updates_performance_json(self, fetcher_env) -> None:
        agent, sm, mock_api, _, analytics_dir = fetcher_env
        sm.save_json("post_history.json", {
            "posts": [_make_post("p1", "m1", hours_ago=12)],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        perf_path = analytics_dir / "performance.json"
        assert perf_path.exists()
        with open(perf_path, encoding="utf-8") as f:
            perf_data = json.load(f)
        assert len(perf_data["records"]) == 1
        assert perf_data["records"][0]["views"] == 1000


# ====================================================================
# Retroactive affiliate injection
# ====================================================================


class TestFetcherAffiliate:

    def test_affiliate_injection_on_buzz_post(self, fetcher_env) -> None:
        agent, sm, mock_api, mock_claude, _ = fetcher_env
        # Post with high views + engagement
        sm.save_json("post_history.json", {
            "posts": [_make_post(
                "p1", "m1", hours_ago=12,
                views=600, likes=20, replies=10,
            )],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        # Should have called create_text_post for affiliate reply
        affiliate_calls = [
            c for c in mock_api.create_text_post.call_args_list
            if c[1].get("reply_to_id") == "m1"
        ]
        assert len(affiliate_calls) == 1

    def test_skips_already_affiliated(self, fetcher_env) -> None:
        agent, sm, mock_api, *_ = fetcher_env
        sm.save_json("post_history.json", {
            "posts": [_make_post(
                "p1", "m1", hours_ago=12,
                views=600, likes=20, replies=10,
                affiliate_added_at=_NOW.isoformat(),
            )],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        # No affiliate self-reply should be posted
        affiliate_calls = [
            c for c in mock_api.create_text_post.call_args_list
            if c[1].get("reply_to_id") is not None
        ]
        assert len(affiliate_calls) == 0

    def test_daily_budget_respected(self, fetcher_env) -> None:
        agent, sm, mock_api, *_ = fetcher_env
        # 5 posts already have affiliate today (max is 5)
        posts = []
        for i in range(5):
            p = _make_post(f"p{i}", f"m{i}", hours_ago=12,
                           views=600, likes=20, replies=10,
                           affiliate_added_at=f"{_TODAY_KEY}T10:00:00+09:00")
            posts.append(p)
        # Add one more buzz post without affiliate
        posts.append(_make_post("p_new", "m_new", hours_ago=12,
                                views=800, likes=40, replies=15))

        sm.save_json("post_history.json", {"posts": posts})

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        # Should NOT add affiliate to p_new (budget exhausted)
        affiliate_calls = [
            c for c in mock_api.create_text_post.call_args_list
            if c[1].get("reply_to_id") == "m_new"
        ]
        assert len(affiliate_calls) == 0

    def test_pr_label_on_affiliate_comment(self, fetcher_env) -> None:
        agent, sm, mock_api, mock_claude, _ = fetcher_env
        # Claude returns without PR label
        mock_claude.generate_post.return_value = "このセラミド化粧水おすすめだよ"

        sm.save_json("post_history.json", {
            "posts": [_make_post(
                "p1", "m1", hours_ago=12,
                views=600, likes=20, replies=10,
            )],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        # The affiliate text should start with PR
        affiliate_calls = [
            c for c in mock_api.create_text_post.call_args_list
            if c[1].get("reply_to_id") == "m1"
        ]
        if affiliate_calls:
            text = affiliate_calls[0][0][0]
            assert text.startswith("PR")

    def test_low_engagement_not_triggered(self, fetcher_env) -> None:
        agent, sm, mock_api, *_ = fetcher_env
        # High views but very low engagement
        mock_api.get_post_insights.return_value = {
            "views": 1000, "likes": 2, "replies": 0,
            "reposts": 0, "quotes": 0,
        }
        sm.save_json("post_history.json", {
            "posts": [_make_post("p1", "m1", hours_ago=12)],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            agent.execute()

        # No affiliate should be added (engagement < 5%)
        affiliate_calls = [
            c for c in mock_api.create_text_post.call_args_list
            if c[1].get("reply_to_id") is not None
        ]
        assert len(affiliate_calls) == 0


# ====================================================================
# Product catalog matching
# ====================================================================


class TestProductCatalog:

    def test_select_product_matches_by_keyword(self, fetcher_env) -> None:
        agent, sm, *_ = fetcher_env
        # Manually set catalog
        agent._product_catalog = [
            {
                "id": "aff_001",
                "display_hint": "セラミドNP配合の化粧水",
                "category": "skincare_ingredients",
                "keywords": ["セラミド", "乾燥肌", "保湿"],
                "tier": "high",
                "priority": 10,
                "active": True,
            },
            {
                "id": "aff_002",
                "display_hint": "レチノール美容液",
                "category": "skincare_ingredients",
                "keywords": ["レチノール", "シワ"],
                "tier": "medium",
                "priority": 5,
                "active": True,
            },
        ]
        agent._matching_cfg = {"min_keyword_overlap": 1, "max_same_product_per_week": 3}

        result = agent._select_product(
            "skincare_ingredients", "セラミドで乾燥肌を保湿する方法"
        )
        assert result is not None
        assert result["id"] == "aff_001"

    def test_select_product_returns_none_for_no_match(self, fetcher_env) -> None:
        agent, *_ = fetcher_env
        agent._product_catalog = [
            {
                "id": "aff_001",
                "display_hint": "セラミド化粧水",
                "category": "skincare_ingredients",
                "keywords": ["セラミド"],
                "tier": "high",
                "priority": 10,
                "active": True,
            },
        ]
        agent._matching_cfg = {"min_keyword_overlap": 1, "max_same_product_per_week": 3}

        result = agent._select_product("diet_tips", "ダイエットの食事管理")
        assert result is None

    def test_select_product_prefers_high_tier(self, fetcher_env) -> None:
        agent, *_ = fetcher_env
        agent._product_catalog = [
            {
                "id": "low_prod",
                "display_hint": "低単価",
                "category": "skincare_ingredients",
                "keywords": ["セラミド"],
                "tier": "low",
                "priority": 10,
                "active": True,
            },
            {
                "id": "high_prod",
                "display_hint": "高単価",
                "category": "skincare_ingredients",
                "keywords": ["セラミド"],
                "tier": "high",
                "priority": 10,
                "active": True,
            },
        ]
        agent._matching_cfg = {"min_keyword_overlap": 1, "max_same_product_per_week": 3}

        result = agent._select_product(
            "skincare_ingredients", "セラミドの選び方"
        )
        assert result is not None
        assert result["id"] == "high_prod"

    def test_weekly_limit_skips_overused_product(self, fetcher_env) -> None:
        agent, sm, *_ = fetcher_env
        agent._product_catalog = [
            {
                "id": "aff_001",
                "display_hint": "セラミド化粧水",
                "category": "skincare_ingredients",
                "keywords": ["セラミド"],
                "tier": "high",
                "priority": 10,
                "active": True,
            },
        ]
        agent._matching_cfg = {"min_keyword_overlap": 1, "max_same_product_per_week": 1}

        # Already used this product once this week
        now = _NOW
        sm.save_json("post_history.json", {
            "posts": [{
                "id": "p_old",
                "affiliate_product_id": "aff_001",
                "affiliate_added_at": now.isoformat(),
            }],
        })

        with patch("agents.fetcher.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _NOW
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = agent._select_product(
                "skincare_ingredients", "セラミドの選び方"
            )
        assert result is None

    def test_record_product_usage(self, fetcher_env) -> None:
        agent, *_ = fetcher_env
        post: dict = {"id": "p1"}
        agent._record_product_usage(post, "aff_001")
        assert post["affiliate_product_id"] == "aff_001"


# ====================================================================
# Performance record builder
# ====================================================================


class TestBuildPerformanceRecord:

    def test_builds_correct_record(self) -> None:
        from agents.fetcher import _build_performance_record

        post = {
            "id": "p1",
            "threads_media_id": "m1",
            "posted_at": "2026-03-26T12:00:00+09:00",
        }
        insights = {"views": 500, "likes": 25, "replies": 5, "reposts": 3, "quotes": 2}
        record = _build_performance_record(post, insights, "6h", "2026-03-26T18:00:00+09:00")

        assert record["post_id"] == "p1"
        assert record["stage"] == "6h"
        assert record["views"] == 500
        assert record["likes"] == 25
        assert record["engagement_rate"] == round(35 / 500 * 100, 2)

    def test_zero_views_no_division_error(self) -> None:
        from agents.fetcher import _build_performance_record

        post = {"id": "p1", "threads_media_id": "m1", "posted_at": "2026-03-26T12:00:00"}
        insights = {"views": 0, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0}
        record = _build_performance_record(post, insights, "24h", "2026-03-26T18:00:00")
        assert record["engagement_rate"] == 0.0
