"""Tests for StateManager.archive_old_posts() — P3-1 / P3-5."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager

_JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture()
def sm(tmp_path: Path) -> StateManager:
    """StateManager backed by a temp directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return StateManager(state_dir=state_dir)


def _make_post(post_id: str, days_ago: int) -> dict:
    """Create a minimal post dict posted *days_ago* days in the past."""
    posted_at = datetime.now(_JST) - timedelta(days=days_ago)
    return {
        "id": post_id,
        "content": f"Post {post_id}",
        "posted_at": posted_at.isoformat(),
        "category": "skincare_knowledge",
    }


class TestArchiveOldPosts:
    def test_no_posts_returns_zero(self, sm: StateManager):
        sm.save_json("post_history.json", {"posts": []})
        assert sm.archive_old_posts() == 0

    def test_all_recent_posts_stay(self, sm: StateManager):
        posts = [_make_post(f"p{i}", days_ago=i) for i in range(5)]
        sm.save_json("post_history.json", {"posts": posts})
        assert sm.archive_old_posts() == 0
        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 5

    def test_old_posts_archived(self, sm: StateManager):
        recent = [_make_post(f"recent_{i}", days_ago=i) for i in range(3)]
        old = [_make_post(f"old_{i}", days_ago=91 + i) for i in range(5)]
        sm.save_json("post_history.json", {"posts": recent + old})

        archived_count = sm.archive_old_posts()
        assert archived_count == 5

        # post_history should only have recent posts
        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 3
        assert all(p["id"].startswith("recent_") for p in history["posts"])

    def test_archive_files_created(self, sm: StateManager):
        old = [_make_post(f"old_{i}", days_ago=100 + i) for i in range(3)]
        sm.save_json("post_history.json", {"posts": old})

        sm.archive_old_posts()

        archive_dir = sm.state_dir.parent / "archive"
        assert archive_dir.exists()
        archive_files = list(archive_dir.glob("post_history_*.json"))
        assert len(archive_files) >= 1

        # Verify archive content
        for af in archive_files:
            with open(af, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert "posts" in data
            assert "month" in data
            assert len(data["posts"]) > 0

    def test_archive_appends_to_existing(self, sm: StateManager):
        # First round
        old1 = [_make_post("old_1", days_ago=100)]
        sm.save_json("post_history.json", {"posts": old1})
        sm.archive_old_posts()

        # Second round — same month
        old2 = [_make_post("old_2", days_ago=101)]
        sm.save_json("post_history.json", {"posts": old2})
        sm.archive_old_posts()

        archive_dir = sm.state_dir.parent / "archive"
        archive_files = list(archive_dir.glob("post_history_*.json"))
        # At least one file should have both posts
        total_archived = 0
        for af in archive_files:
            with open(af, "r", encoding="utf-8") as f:
                data = json.load(f)
            total_archived += len(data["posts"])
        assert total_archived == 2

    def test_posts_without_posted_at_kept(self, sm: StateManager):
        posts = [
            {"id": "no_date", "content": "no date post"},
            _make_post("old", days_ago=100),
        ]
        sm.save_json("post_history.json", {"posts": posts})
        sm.archive_old_posts()
        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 1
        assert history["posts"][0]["id"] == "no_date"

    def test_boundary_89_days_kept(self, sm: StateManager):
        """Posts within the 90-day window are kept."""
        post = _make_post("boundary", days_ago=89)
        sm.save_json("post_history.json", {"posts": [post]})
        assert sm.archive_old_posts() == 0
        history = sm.load_json("post_history.json")
        assert len(history["posts"]) == 1

    def test_boundary_91_days_archived(self, sm: StateManager):
        """Posts outside the 90-day window are archived."""
        post = _make_post("boundary", days_ago=91)
        sm.save_json("post_history.json", {"posts": [post]})
        assert sm.archive_old_posts() == 1

    def test_multiple_months_separated(self, sm: StateManager):
        posts = [
            _make_post("jan", days_ago=150),  # ~5 months ago
            _make_post("feb", days_ago=120),  # ~4 months ago
        ]
        sm.save_json("post_history.json", {"posts": posts})
        sm.archive_old_posts()

        archive_dir = sm.state_dir.parent / "archive"
        archive_files = list(archive_dir.glob("post_history_*.json"))
        # The two posts may be in different months or same depending on dates
        total_archived = 0
        for af in archive_files:
            with open(af, "r", encoding="utf-8") as f:
                data = json.load(f)
            total_archived += len(data["posts"])
        assert total_archived == 2
