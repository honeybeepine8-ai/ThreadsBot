"""Tests for core.state_manager.StateManager."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from core.state_manager import StateManager


class TestLoadDefaultSchema:
    """load_json should return the correct default when the file does not exist."""

    def test_known_file_returns_default(self, state_manager: StateManager) -> None:
        data = state_manager.load_json("system_state.json")
        assert data.get("emergency_stop") is False, (
            "Default system_state should have emergency_stop=False"
        )
        assert "daily_counters" in data, (
            "Default system_state should contain daily_counters key"
        )

    def test_unknown_file_returns_empty_dict(self, state_manager: StateManager) -> None:
        data = state_manager.load_json("nonexistent_file.json")
        assert data == {}, "Unknown file should return an empty dict"

    def test_post_history_default(self, state_manager: StateManager) -> None:
        data = state_manager.load_json("post_history.json")
        assert data["posts"] == [], "Default post_history should have an empty posts list"
        assert data["last_updated"] is None, "Default last_updated should be None"


class TestSaveAndLoad:
    """save_json -> load_json round-trip should preserve data."""

    def test_round_trip(self, state_manager: StateManager) -> None:
        payload = {"foo": "bar", "nested": {"count": 42, "items": [1, 2, 3]}}
        state_manager.save_json("test.json", payload)
        loaded = state_manager.load_json("test.json")
        assert loaded == payload, "Loaded data should match the saved payload exactly"

    def test_overwrite(self, state_manager: StateManager) -> None:
        state_manager.save_json("test.json", {"version": 1})
        state_manager.save_json("test.json", {"version": 2})
        loaded = state_manager.load_json("test.json")
        assert loaded["version"] == 2, "Overwritten file should reflect the latest data"

    def test_unicode_content(self, state_manager: StateManager) -> None:
        payload = {"message": "日本語テスト🎉"}
        state_manager.save_json("unicode.json", payload)
        loaded = state_manager.load_json("unicode.json")
        assert loaded["message"] == "日本語テスト🎉", "Unicode content should survive round-trip"


class TestAtomicWrite:
    """Atomic write guarantees: existing data must survive a failed write."""

    def test_existing_file_survives_write_failure(
        self, state_manager: StateManager
    ) -> None:
        # First, write a good payload
        original = {"status": "ok", "count": 10}
        state_manager.save_json("important.json", original)

        # Simulate a failure during os.replace by raising OSError
        with patch("os.replace", side_effect=OSError("disk full")):
            try:
                state_manager.save_json("important.json", {"status": "corrupt"})
            except OSError:
                pass  # expected

        # The original file should still be intact
        loaded = state_manager.load_json("important.json")
        assert loaded == original, (
            "Original data must remain intact after a failed write"
        )

    def test_no_leftover_temp_files(self, state_manager: StateManager) -> None:
        state_manager.save_json("clean.json", {"a": 1})
        tmp_files = list(state_manager.state_dir.glob("*.tmp"))
        assert tmp_files == [], "No temporary files should remain after a successful save"
