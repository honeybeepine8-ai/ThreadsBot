"""Tests for scripts/backup.py — P3-2 / P3-6."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def backup_env(tmp_path: Path):
    """Set up state and backup directories for testing."""
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)

    # Create some sample state files
    for name in ["post_history.json", "system_state.json", "post_queue.json"]:
        (state_dir / name).write_text(
            json.dumps({"test": True}), encoding="utf-8"
        )

    return state_dir, backup_dir


class TestRunBackup:
    def test_creates_zip(self, backup_env):
        state_dir, backup_dir = backup_env
        with patch("scripts.backup.STATE_DIR", state_dir), \
             patch("scripts.backup.BACKUP_DIR", backup_dir):
            from scripts.backup import run_backup
            result = run_backup()

        assert result is not None
        assert result.exists()
        assert result.suffix == ".zip"

    def test_dry_run_no_zip(self, backup_env):
        state_dir, backup_dir = backup_env
        with patch("scripts.backup.STATE_DIR", state_dir), \
             patch("scripts.backup.BACKUP_DIR", backup_dir):
            from scripts.backup import run_backup
            result = run_backup(dry_run=True)

        assert result is None
        zips = list(backup_dir.glob("state_*.zip"))
        assert len(zips) == 0

    def test_missing_state_dir(self, tmp_path: Path):
        missing = tmp_path / "nonexistent"
        with patch("scripts.backup.STATE_DIR", missing):
            from scripts.backup import run_backup
            result = run_backup()
        assert result is None


class TestRotation:
    def test_rotation_keeps_max_generations(self, backup_env):
        state_dir, backup_dir = backup_env

        # Create 32 fake backups
        for i in range(32):
            (backup_dir / f"state_2026010{i:02d}_040000.zip").write_bytes(b"fake")

        with patch("scripts.backup.STATE_DIR", state_dir), \
             patch("scripts.backup.BACKUP_DIR", backup_dir), \
             patch("scripts.backup.MAX_GENERATIONS", 30):
            from scripts.backup import _rotate_backups
            deleted = _rotate_backups()

        assert deleted == 2
        remaining = list(backup_dir.glob("state_*.zip"))
        assert len(remaining) == 30

    def test_no_rotation_under_limit(self, backup_env):
        _, backup_dir = backup_env

        for i in range(5):
            (backup_dir / f"state_2026010{i}_040000.zip").write_bytes(b"fake")

        with patch("scripts.backup.BACKUP_DIR", backup_dir), \
             patch("scripts.backup.MAX_GENERATIONS", 30):
            from scripts.backup import _rotate_backups
            deleted = _rotate_backups()

        assert deleted == 0
