"""Account context — resolves file paths relative to the project root.

Provides a single consistent interface for loading config, knowledge,
and state files.  Always resolves to the project root directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

import yaml
from dotenv import load_dotenv

from core.logger import get_logger
from core.state_manager import StateManager

logger = get_logger("account_context")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AccountContext:
    """Resolve file paths and load configuration for the project.

    All paths resolve to the project root directory.
    """

    PROJECT_ROOT: ClassVar[Path] = _PROJECT_ROOT

    def __init__(self, account_id: str = "default") -> None:
        self.account_id = "default"
        self.root_dir = _PROJECT_ROOT
        self._state_manager: StateManager | None = None

    # ------------------------------------------------------------------
    # Directory properties
    # ------------------------------------------------------------------

    @property
    def config_dir(self) -> Path:
        return self.root_dir / "config"

    @property
    def knowledge_dir(self) -> Path:
        return self.root_dir / "knowledge"

    @property
    def prompts_dir(self) -> Path:
        return self.root_dir / "prompts"

    @property
    def state_dir(self) -> Path:
        return self.root_dir / "data" / "state"

    @property
    def analytics_dir(self) -> Path:
        return self.root_dir / "data" / "analytics"

    @property
    def archive_dir(self) -> Path:
        return self.root_dir / "data" / "archive"

    @property
    def backup_dir(self) -> Path:
        return self.root_dir / "data" / "backups"

    @property
    def env_path(self) -> Path:
        return self.root_dir / ".env"

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve_path(self, relative_path: str) -> Path:
        """Return the absolute path for *relative_path* under project root.

        Raises ``FileNotFoundError`` if the file does not exist.
        """
        path = self.root_dir / relative_path
        if path.exists():
            return path
        raise FileNotFoundError(f"File not found: {relative_path}")

    # ------------------------------------------------------------------
    # File loading helpers
    # ------------------------------------------------------------------

    def load_yaml(self, relative_path: str) -> dict[str, Any]:
        """Load a YAML file, resolving via :meth:`resolve_path`."""
        path = self.resolve_path(relative_path)
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def load_text(self, relative_path: str) -> str:
        """Load a text file, resolving via :meth:`resolve_path`."""
        path = self.resolve_path(relative_path)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def load_settings(self) -> dict[str, Any]:
        """Load settings.yaml from the project config directory."""
        path = _PROJECT_ROOT / "config" / "settings.yaml"
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    def get_state_manager(self) -> StateManager:
        """Return (cached) :class:`StateManager` for this account."""
        if self._state_manager is None:
            self._state_manager = StateManager(state_dir=self.state_dir)
        return self._state_manager

    def load_env(self) -> None:
        """Load the project ``.env`` file into ``os.environ``."""
        if self.env_path.exists():
            load_dotenv(self.env_path, override=True)

    def __repr__(self) -> str:
        return f"AccountContext(root={self.root_dir})"
