"""Account context for multi-account template architecture.

Resolves file paths per account. The "default" account maps to the
project root for full backward compatibility.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, ClassVar

import yaml
from dotenv import load_dotenv

from core.logger import get_logger
from core.state_manager import StateManager

logger = get_logger("account_context")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# System-level keys in settings.yaml that cannot be overridden per-account.
_SYSTEM_KEYS: frozenset[str] = frozenset(
    {"app", "claude", "threads_api", "notifications"}
)


class AccountContext:
    """Resolve file paths and load configuration for a specific account.

    * ``account_id="default"`` resolves every path to the project root
      (identical to the pre-AccountContext hard-coded paths).
    * Any other ``account_id`` resolves to ``accounts/<account_id>/``
      with automatic fallback to the project root when a file does not
      exist in the account directory.
    """

    PROJECT_ROOT: ClassVar[Path] = _PROJECT_ROOT

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, account_id: str = "default") -> None:
        self.account_id = account_id

        if account_id == "default":
            self.root_dir = _PROJECT_ROOT
        else:
            self.root_dir = _PROJECT_ROOT / "accounts" / account_id
            if not self.root_dir.exists():
                raise ValueError(
                    f"Account directory not found: {self.root_dir}"
                )

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
    # Path resolution (account-first, project-root fallback)
    # ------------------------------------------------------------------

    def resolve_path(self, relative_path: str) -> Path:
        """Return the account-local path if it exists, else the project-root path.

        Raises ``FileNotFoundError`` if neither location contains the file.
        """
        account_path = self.root_dir / relative_path
        if account_path.exists():
            return account_path

        if self.account_id != "default":
            fallback = _PROJECT_ROOT / relative_path
            if fallback.exists():
                return fallback

        raise FileNotFoundError(
            f"File not found for account '{self.account_id}': {relative_path}"
        )

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

    # ------------------------------------------------------------------
    # Settings merge
    # ------------------------------------------------------------------

    def load_settings(self) -> dict[str, Any]:
        """Load merged settings: system defaults + account overrides.

        System-level keys (``app``, ``claude``, ``threads_api``,
        ``notifications``) are always taken from the project-root
        ``config/settings.yaml`` and cannot be overridden per-account.
        """
        system_path = _PROJECT_ROOT / "config" / "settings.yaml"
        with open(system_path, encoding="utf-8") as f:
            base: dict[str, Any] = yaml.safe_load(f) or {}

        if self.account_id == "default":
            return base

        account_settings = self.config_dir / "settings.yaml"
        if not account_settings.exists():
            return base

        with open(account_settings, encoding="utf-8") as f:
            overrides: dict[str, Any] = yaml.safe_load(f) or {}

        merged = copy.deepcopy(base)
        for key, value in overrides.items():
            if key in _SYSTEM_KEYS:
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value

        return merged

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    def get_state_manager(self) -> StateManager:
        """Return (cached) :class:`StateManager` for this account."""
        if self._state_manager is None:
            self._state_manager = StateManager(state_dir=self.state_dir)
        return self._state_manager

    def load_env(self) -> None:
        """Load the account-specific ``.env`` file into ``os.environ``."""
        if self.env_path.exists():
            load_dotenv(self.env_path, override=True)
            logger.debug("Loaded .env for account '%s'", self.account_id)

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"AccountContext(account_id={self.account_id!r}, root={self.root_dir})"
