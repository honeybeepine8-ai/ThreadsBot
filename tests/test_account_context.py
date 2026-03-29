"""Tests for core.account_context.AccountContext."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from core.account_context import AccountContext


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def template_dir() -> Path:
    """Return the real _template directory path."""
    return AccountContext.PROJECT_ROOT / "accounts" / "_template"


@pytest.fixture()
def test_account(tmp_path: Path) -> tuple[str, Path]:
    """Create a temporary test account by copying the template.

    Returns (account_id, account_dir).
    """
    account_id = "test_acct"
    accounts_dir = AccountContext.PROJECT_ROOT / "accounts"
    target = accounts_dir / account_id

    # Copy template
    template = accounts_dir / "_template"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(template, target)

    # Rename .env.example -> .env
    env_example = target / ".env.example"
    if env_example.exists():
        env_example.rename(target / ".env")

    # Ensure data dirs
    (target / "data" / "state").mkdir(parents=True, exist_ok=True)

    yield account_id, target

    # Cleanup
    if target.exists():
        shutil.rmtree(target)


# ------------------------------------------------------------------
# Default account tests
# ------------------------------------------------------------------


class TestDefaultAccount:
    def test_default_root_is_project_root(self):
        ctx = AccountContext()
        assert ctx.root_dir == AccountContext.PROJECT_ROOT

    def test_default_account_id(self):
        ctx = AccountContext("default")
        assert ctx.account_id == "default"

    def test_config_dir(self):
        ctx = AccountContext()
        assert ctx.config_dir == AccountContext.PROJECT_ROOT / "config"

    def test_knowledge_dir(self):
        ctx = AccountContext()
        assert ctx.knowledge_dir == AccountContext.PROJECT_ROOT / "knowledge"

    def test_prompts_dir(self):
        ctx = AccountContext()
        assert ctx.prompts_dir == AccountContext.PROJECT_ROOT / "prompts"

    def test_state_dir(self):
        ctx = AccountContext()
        assert ctx.state_dir == AccountContext.PROJECT_ROOT / "data" / "state"

    def test_analytics_dir(self):
        ctx = AccountContext()
        assert ctx.analytics_dir == AccountContext.PROJECT_ROOT / "data" / "analytics"

    def test_env_path(self):
        ctx = AccountContext()
        assert ctx.env_path == AccountContext.PROJECT_ROOT / ".env"


# ------------------------------------------------------------------
# Path resolution tests
# ------------------------------------------------------------------


class TestResolve:
    def test_resolve_existing_file(self):
        ctx = AccountContext()
        path = ctx.resolve_path("config/settings.yaml")
        assert path.exists()
        assert path == AccountContext.PROJECT_ROOT / "config" / "settings.yaml"

    def test_resolve_nonexistent_raises(self):
        ctx = AccountContext()
        with pytest.raises(FileNotFoundError):
            ctx.resolve_path("config/nonexistent_file_xyz.yaml")

    def test_resolve_fallback_to_project_root(self, test_account):
        """Account dir without the file falls back to project root."""
        account_id, account_dir = test_account

        # Remove settings.yaml from account to test fallback
        account_settings = account_dir / "config" / "settings.yaml"
        if account_settings.exists():
            account_settings.unlink()

        ctx = AccountContext(account_id)
        path = ctx.resolve_path("config/settings.yaml")
        assert path == AccountContext.PROJECT_ROOT / "config" / "settings.yaml"

    def test_resolve_prefers_account_dir(self, test_account):
        """Account-local file takes precedence over project root."""
        account_id, account_dir = test_account
        ctx = AccountContext(account_id)
        path = ctx.resolve_path("config/tone.yaml")
        assert path == account_dir / "config" / "tone.yaml"


# ------------------------------------------------------------------
# File loading tests
# ------------------------------------------------------------------


class TestLoadFiles:
    def test_load_yaml_returns_dict(self):
        ctx = AccountContext()
        data = ctx.load_yaml("config/settings.yaml")
        assert isinstance(data, dict)
        assert "safety" in data

    def test_load_text_returns_string(self):
        ctx = AccountContext()
        text = ctx.load_text("prompts/writer.md")
        assert isinstance(text, str)
        assert len(text) > 0

    def test_load_yaml_nonexistent_raises(self):
        ctx = AccountContext()
        with pytest.raises(FileNotFoundError):
            ctx.load_yaml("config/nonexistent_xyz.yaml")


# ------------------------------------------------------------------
# Settings merge tests
# ------------------------------------------------------------------


class TestSettingsMerge:
    def test_default_returns_full_settings(self):
        ctx = AccountContext()
        settings = ctx.load_settings()
        assert "app" in settings
        assert "safety" in settings
        assert "claude" in settings

    def test_account_overrides_non_system_keys(self, test_account):
        account_id, account_dir = test_account

        # Write a custom settings.yaml with overrides
        custom = {
            "safety": {"max_daily_posts": 99},
            "writer": {"queue_target_size": 42},
        }
        settings_path = account_dir / "config" / "settings.yaml"
        with open(settings_path, "w", encoding="utf-8") as f:
            yaml.dump(custom, f)

        ctx = AccountContext(account_id)
        settings = ctx.load_settings()

        # Overridden values
        assert settings["safety"]["max_daily_posts"] == 99
        assert settings["writer"]["queue_target_size"] == 42

        # System keys preserved from project root
        assert "app" in settings
        assert "claude" in settings

    def test_system_keys_cannot_be_overridden(self, test_account):
        account_id, account_dir = test_account

        # Try to override system key
        custom = {"claude": {"writer_model": "hacked-model"}}
        settings_path = account_dir / "config" / "settings.yaml"
        with open(settings_path, "w", encoding="utf-8") as f:
            yaml.dump(custom, f)

        ctx = AccountContext(account_id)
        settings = ctx.load_settings()

        # System key should be unchanged
        root_ctx = AccountContext()
        root_settings = root_ctx.load_settings()
        assert settings["claude"] == root_settings["claude"]


# ------------------------------------------------------------------
# StateManager integration tests
# ------------------------------------------------------------------


class TestStateManager:
    def test_get_state_manager_returns_correct_dir(self):
        ctx = AccountContext()
        sm = ctx.get_state_manager()
        assert sm.state_dir == ctx.state_dir

    def test_get_state_manager_cached(self):
        ctx = AccountContext()
        sm1 = ctx.get_state_manager()
        sm2 = ctx.get_state_manager()
        assert sm1 is sm2

    def test_account_state_manager_isolation(self, test_account):
        account_id, account_dir = test_account
        ctx = AccountContext(account_id)
        sm = ctx.get_state_manager()
        assert sm.state_dir == account_dir / "data" / "state"


# ------------------------------------------------------------------
# Error handling tests
# ------------------------------------------------------------------


class TestErrors:
    def test_nonexistent_account_raises(self):
        with pytest.raises(ValueError, match="Account directory not found"):
            AccountContext("nonexistent_account_xyz_123")

    def test_repr(self):
        ctx = AccountContext()
        r = repr(ctx)
        assert "default" in r
        assert "AccountContext" in r
