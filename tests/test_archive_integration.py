"""Archive integration tests — end-to-end archive flows (P4-4).

Tests archive mechanism with realistic data and verifies
that downstream consumers (quality_gate similarity check) are unaffected.
"""

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
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return StateManager(state_dir=state_dir)


def _build_realistic_history(sm: StateManager) -> None:
    """Create 6 months of posts (6/day = ~1080 posts)."""
    now = datetime.now(_JST)
    posts = []
    for day_offset in range(180):
        date = now - timedelta(days=day_offset)
        for i in range(6):
            posts.append({
                "id": f"post_{date.strftime('%Y%m%d')}_{i + 1:03d}",
                "content": f"Skincare tip #{day_offset * 6 + i}",
                "posted_at": date.replace(hour=8 + i * 2).isoformat(),
                "category": "skincare_knowledge",
                "pattern": "短文完結型",
                "quality_score": 8.0 + (i % 3) * 0.5,
                "metrics": {
                    "views": 100 + i * 50,
                    "likes": 5 + i,
                },
            })
    sm.save_json("post_history.json", {
        "posts": posts,
        "daily_stats": {},
    })
    return len(posts)


class TestArchiveIntegration:
    def test_large_history_archived(self, sm: StateManager):
        total = _build_realistic_history(sm)
        assert total == 1080

        archived = sm.archive_old_posts()

        # Should archive posts older than 90 days
        # 90 days * 6 posts/day = ~540 posts kept
        history = sm.load_json("post_history.json")
        kept = len(history["posts"])
        assert kept < total
        assert kept <= 90 * 6 + 6  # ~540 + buffer for timing
        assert archived == total - kept

    def test_archive_preserves_recent_for_similarity(self, sm: StateManager):
        _build_realistic_history(sm)
        sm.archive_old_posts()

        history = sm.load_json("post_history.json")
        posts = history["posts"]

        # Quality gate checks last 100 posts — all should be present
        assert len(posts) >= 100

        # All remaining posts should be within 90 days
        cutoff = datetime.now(_JST) - timedelta(days=91)
        for post in posts:
            posted_at = datetime.fromisoformat(post["posted_at"])
            assert posted_at > cutoff

    def test_archive_files_organized_by_month(self, sm: StateManager):
        _build_realistic_history(sm)
        sm.archive_old_posts()

        archive_dir = sm.state_dir.parent / "archive"
        assert archive_dir.exists()

        archive_files = sorted(archive_dir.glob("post_history_*.json"))
        assert len(archive_files) >= 2  # At least 2 months of old data

        for af in archive_files:
            with open(af, encoding="utf-8") as f:
                data = json.load(f)
            assert "month" in data
            assert "posts" in data
            # Verify all posts belong to the correct month
            month = data["month"]
            for post in data["posts"]:
                post_month = post["posted_at"][:7]  # YYYY-MM
                assert post_month == month

    def test_repeated_archive_is_idempotent(self, sm: StateManager):
        _build_realistic_history(sm)

        first_run = sm.archive_old_posts()
        assert first_run > 0

        # Second run should find nothing to archive
        second_run = sm.archive_old_posts()
        assert second_run == 0

    def test_json_corruption_recovery(self, sm: StateManager):
        """Test that corrupted JSON is backed up and reset."""
        # Write valid data first
        sm.save_json("post_history.json", {"posts": [{"id": "test"}]})

        # Corrupt the file
        path = sm.state_dir / "post_history.json"
        path.write_text("{invalid json!!!", encoding="utf-8")

        # Load should return default and create .bak
        data = sm.load_json("post_history.json")
        assert data == {"last_updated": None, "posts": [], "daily_stats": {}}

        # Check .bak file exists
        bak_files = list(sm.state_dir.glob("post_history.bak.*.json"))
        assert len(bak_files) == 1
