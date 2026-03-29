"""E2E integration tests — metrics-to-analysis pipeline (P4-2).

Tests: post_history → Fetcher → performance.json → Analyst → audience.json
All external APIs (Claude, Threads) are mocked.
"""

from __future__ import annotations

import datetime
import json
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
    # Create analytics directory
    analytics_dir = tmp_path / "analytics"
    analytics_dir.mkdir()
    return StateManager(state_dir=state_dir)


def _seed_posted_posts(sm: StateManager, hours_ago: int = 8, count: int = 3) -> None:
    """Create posts that are ready for metrics fetching."""
    now = datetime.datetime.now(_JST)
    posts = []
    for i in range(count):
        posted_at = now - datetime.timedelta(hours=hours_ago + i)
        posts.append({
            "id": f"post_{now.strftime('%Y%m%d')}_{i + 1:03d}",
            "threads_media_id": f"media_{i + 1}",
            "content": f"Test post {i + 1} about skincare",
            "pattern": "短文完結型",
            "category": "skincare_knowledge",
            "quality_score": 8.5,
            "posted_at": posted_at.isoformat(),
            "metrics": {
                "views": 0, "likes": 0, "replies": 0,
                "reposts": 0, "quotes": 0, "last_fetched": None,
            },
        })
    sm.save_json("post_history.json", {
        "posts": posts, "daily_stats": {},
    })


def _seed_system(sm: StateManager) -> None:
    sm.save_json("system_state.json", {
        "emergency_stop": False,
        "agent_status": {},
        "daily_counters": {},
    })


def _mock_threads_api_with_insights():
    mock = MagicMock()
    mock.get_post_insights.return_value = {
        "views": 500, "likes": 25, "replies": 5,
        "reposts": 3, "quotes": 1,
    }
    mock.create_text_post.return_value = {"id": "aff_media_1"}
    return mock


class TestMetricsCollection:
    """Fetcher collects metrics from Threads API."""

    @patch("agents.fetcher.ClaudeClient")
    @patch("agents.fetcher.ThreadsAPIClient")
    @patch("core.notifier.Notifier.send", return_value=False)
    def test_fetcher_updates_metrics(
        self, mock_notify, MockThreads, MockClaude, sm: StateManager
    ):
        _seed_posted_posts(sm, hours_ago=8)
        _seed_system(sm)

        mock_threads = _mock_threads_api_with_insights()
        MockThreads.return_value = mock_threads
        MockClaude.return_value = MagicMock()

        from agents.fetcher import FetcherAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(FetcherAgent, "__init__", lambda self: None):
            fetcher = FetcherAgent.__new__(FetcherAgent)
            fetcher.name = "fetcher"
            fetcher.state = sm
            fetcher.logger = get_logger("fetcher")
            fetcher.safety = SafetyGuard(sm)
            fetcher.threads = mock_threads
            fetcher.claude_client = MockClaude.return_value
            fetcher.notifier = Notifier()
            fetcher.affiliate_mode = "retroactive"
            fetcher.buzz_views = 500
            fetcher.buzz_engagement = 0.05
            fetcher.max_affiliate_per_day = 5
            fetcher.affiliate_cooldown_hours = 6
            fetcher._product_catalog = None
            fetcher._matching_cfg = {}

        # Patch the analytics directory
        project_root = sm.state_dir.parent
        analytics_dir = project_root / "analytics"
        analytics_dir.mkdir(exist_ok=True)
        knowledge_dir = project_root / "knowledge"
        knowledge_dir.mkdir(exist_ok=True)
        with patch("agents.fetcher._ANALYTICS_DIR", analytics_dir), \
             patch("agents.fetcher._PROJECT_ROOT", project_root):
            fetcher.execute()

        # Verify metrics updated in post_history
        history = sm.load_json("post_history.json")
        for post in history["posts"]:
            metrics = post.get("metrics", {})
            assert metrics.get("views") == 500
            assert metrics.get("likes") == 25
            assert metrics.get("current_stage") == "6h"

    @patch("agents.fetcher.ClaudeClient")
    @patch("agents.fetcher.ThreadsAPIClient")
    @patch("core.notifier.Notifier.send", return_value=False)
    def test_too_recent_posts_skipped(
        self, mock_notify, MockThreads, MockClaude, sm: StateManager
    ):
        # Posts only 2 hours old — too recent for 6h stage
        _seed_posted_posts(sm, hours_ago=2)
        _seed_system(sm)

        mock_threads = _mock_threads_api_with_insights()
        MockThreads.return_value = mock_threads
        MockClaude.return_value = MagicMock()

        from agents.fetcher import FetcherAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(FetcherAgent, "__init__", lambda self: None):
            fetcher = FetcherAgent.__new__(FetcherAgent)
            fetcher.name = "fetcher"
            fetcher.state = sm
            fetcher.logger = get_logger("fetcher")
            fetcher.safety = SafetyGuard(sm)
            fetcher.threads = mock_threads
            fetcher.claude_client = MockClaude.return_value
            fetcher.notifier = Notifier()
            fetcher.affiliate_mode = "retroactive"
            fetcher.buzz_views = 500
            fetcher.buzz_engagement = 0.05
            fetcher.max_affiliate_per_day = 5
            fetcher.affiliate_cooldown_hours = 6
            fetcher._product_catalog = None
            fetcher._matching_cfg = {}

        analytics_dir = sm.state_dir.parent / "analytics"
        analytics_dir.mkdir(exist_ok=True)
        with patch("agents.fetcher._ANALYTICS_DIR", analytics_dir), \
             patch("agents.fetcher._PROJECT_ROOT", sm.state_dir.parent):
            fetcher.execute()

        # Metrics should not be fetched (too recent)
        history = sm.load_json("post_history.json")
        for post in history["posts"]:
            assert post["metrics"]["views"] == 0


class TestPerformanceWrite:
    """Fetcher writes to performance.json."""

    @patch("agents.fetcher.ClaudeClient")
    @patch("agents.fetcher.ThreadsAPIClient")
    @patch("core.notifier.Notifier.send", return_value=False)
    def test_performance_json_created(
        self, mock_notify, MockThreads, MockClaude, sm: StateManager
    ):
        _seed_posted_posts(sm, hours_ago=8, count=2)
        _seed_system(sm)

        mock_threads = _mock_threads_api_with_insights()
        MockThreads.return_value = mock_threads
        MockClaude.return_value = MagicMock()

        from agents.fetcher import FetcherAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(FetcherAgent, "__init__", lambda self: None):
            fetcher = FetcherAgent.__new__(FetcherAgent)
            fetcher.name = "fetcher"
            fetcher.state = sm
            fetcher.logger = get_logger("fetcher")
            fetcher.safety = SafetyGuard(sm)
            fetcher.threads = mock_threads
            fetcher.claude_client = MockClaude.return_value
            fetcher.notifier = Notifier()
            fetcher.affiliate_mode = "disabled"
            fetcher.buzz_views = 500
            fetcher.buzz_engagement = 0.05
            fetcher.max_affiliate_per_day = 5
            fetcher.affiliate_cooldown_hours = 6
            fetcher._product_catalog = None
            fetcher._matching_cfg = {}

        project_root = sm.state_dir.parent
        analytics_dir = project_root / "analytics"
        analytics_dir.mkdir(exist_ok=True)
        knowledge_dir = project_root / "knowledge"
        knowledge_dir.mkdir(exist_ok=True)
        with patch("agents.fetcher._ANALYTICS_DIR", analytics_dir), \
             patch("agents.fetcher._PROJECT_ROOT", project_root):
            fetcher.execute()

        perf_path = analytics_dir / "performance.json"
        assert perf_path.exists()
        with open(perf_path, encoding="utf-8") as f:
            perf = json.load(f)
        assert len(perf["records"]) == 2
        assert perf["records"][0]["views"] == 500


class TestFetchFailure:
    """Fetcher handles API failures gracefully."""

    @patch("agents.fetcher.ClaudeClient")
    @patch("agents.fetcher.ThreadsAPIClient")
    @patch("core.notifier.Notifier.send", return_value=False)
    def test_api_failure_marks_unfetchable(
        self, mock_notify, MockThreads, MockClaude, sm: StateManager
    ):
        _seed_posted_posts(sm, hours_ago=8, count=1)
        _seed_system(sm)

        from services.threads_api import ThreadsAPIError
        mock_threads = MagicMock()
        mock_threads.get_post_insights.side_effect = ThreadsAPIError(
            "HTTP 404: Post does not exist"
        )
        MockThreads.return_value = mock_threads
        MockClaude.return_value = MagicMock()

        from agents.fetcher import FetcherAgent
        from core.logger import get_logger
        from core.safety import SafetyGuard
        from core.notifier import Notifier

        with patch.object(FetcherAgent, "__init__", lambda self: None):
            fetcher = FetcherAgent.__new__(FetcherAgent)
            fetcher.name = "fetcher"
            fetcher.state = sm
            fetcher.logger = get_logger("fetcher")
            fetcher.safety = SafetyGuard(sm)
            fetcher.threads = mock_threads
            fetcher.claude_client = MockClaude.return_value
            fetcher.notifier = Notifier()
            fetcher.affiliate_mode = "disabled"
            fetcher.buzz_views = 500
            fetcher.buzz_engagement = 0.05
            fetcher.max_affiliate_per_day = 5
            fetcher.affiliate_cooldown_hours = 6
            fetcher._product_catalog = None
            fetcher._matching_cfg = {}

        analytics_dir = sm.state_dir.parent / "analytics"
        analytics_dir.mkdir(exist_ok=True)
        with patch("agents.fetcher._ANALYTICS_DIR", analytics_dir), \
             patch("agents.fetcher._PROJECT_ROOT", sm.state_dir.parent):
            fetcher.execute()

        history = sm.load_json("post_history.json")
        post = history["posts"][0]
        assert post["metrics"].get("fetch_status") == "unfetchable"
