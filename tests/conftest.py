"""Shared fixtures for ThreadsBot test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.state_manager import StateManager
from core.safety import SafetyGuard
from core.quality_gate import QualityGate


@pytest.fixture()
def tmp_state_dir(tmp_path: Path) -> Path:
    """Return a temporary directory to be used as the state directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture()
def state_manager(tmp_state_dir: Path) -> StateManager:
    """Return a StateManager backed by a temporary directory."""
    return StateManager(state_dir=tmp_state_dir)


@pytest.fixture()
def safety_guard(state_manager: StateManager) -> SafetyGuard:
    """Return a SafetyGuard wired to the test StateManager.

    The real ``config/settings.yaml`` is loaded so that threshold values
    match production behaviour.
    """
    return SafetyGuard(state_manager=state_manager)


@pytest.fixture()
def quality_gate() -> QualityGate:
    """Return a fresh QualityGate instance."""
    return QualityGate()
