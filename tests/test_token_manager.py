"""Tests for services.token_manager — OAuth token lifecycle management."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from core.state_manager import StateManager
from services.token_manager import TokenManager, TokenRefreshError


@pytest.fixture()
def token_mgr(tmp_path: Path) -> TokenManager:
    """Return a TokenManager backed by a temporary state directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sm = StateManager(state_dir=state_dir)
    return TokenManager(state_manager=sm)


class TestGetValidToken:
    """Tests for TokenManager.get_valid_token."""

    def test_returns_env_token_when_no_state(self, token_mgr: TokenManager) -> None:
        """When no token_state.json exists, use the env variable and initialise state."""
        with patch.dict("os.environ", {"THREADS_ACCESS_TOKEN": "env_token_123"}):
            token = token_mgr.get_valid_token()

        assert token == "env_token_123"

        # State should now be initialised
        state = token_mgr.sm.load_json(TokenManager.TOKEN_STATE_FILE)
        assert state["access_token"] == "env_token_123"
        assert state["expires_at"] is not None

    def test_returns_cached_token_when_not_expiring(self, token_mgr: TokenManager) -> None:
        """When the token has plenty of time left, return it without refreshing."""
        future = (datetime.now(ZoneInfo("Asia/Tokyo")) + timedelta(days=30)).isoformat()
        token_mgr.sm.save_json(TokenManager.TOKEN_STATE_FILE, {
            "access_token": "cached_token",
            "refreshed_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            "expires_at": future,
        })

        token = token_mgr.get_valid_token()
        assert token == "cached_token"

    def test_refreshes_when_expiring_soon(self, token_mgr: TokenManager) -> None:
        """When the token expires within the threshold, attempt a refresh."""
        soon = (datetime.now(ZoneInfo("Asia/Tokyo")) + timedelta(days=3)).isoformat()
        token_mgr.sm.save_json(TokenManager.TOKEN_STATE_FILE, {
            "access_token": "old_token",
            "refreshed_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            "expires_at": soon,
        })

        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "new_token"}
        mock_response.raise_for_status = MagicMock()

        with patch("services.token_manager.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            with patch.object(TokenManager, "_update_env_file"):
                token = token_mgr.get_valid_token()

        assert token == "new_token"

        # State should be updated
        state = token_mgr.sm.load_json(TokenManager.TOKEN_STATE_FILE)
        assert state["access_token"] == "new_token"

    def test_raises_when_no_token_available(self, token_mgr: TokenManager) -> None:
        """When no token is in state or env, raise an error."""
        with patch.dict("os.environ", {}, clear=True):
            # Remove THREADS_ACCESS_TOKEN if it exists
            import os
            os.environ.pop("THREADS_ACCESS_TOKEN", None)
            with pytest.raises(TokenRefreshError, match="No access token"):
                token_mgr.get_valid_token()


class TestGetTokenStatus:
    """Tests for TokenManager.get_token_status."""

    def test_unknown_when_no_state(self, token_mgr: TokenManager) -> None:
        status = token_mgr.get_token_status()
        assert status["status"] == "unknown"
        assert status["needs_refresh"] is True

    def test_valid_status(self, token_mgr: TokenManager) -> None:
        future = (datetime.now(ZoneInfo("Asia/Tokyo")) + timedelta(days=30)).isoformat()
        token_mgr.sm.save_json(TokenManager.TOKEN_STATE_FILE, {
            "access_token": "t",
            "refreshed_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            "expires_at": future,
        })
        status = token_mgr.get_token_status()
        assert status["status"] == "valid"
        assert status["needs_refresh"] is False
        assert status["days_remaining"] >= 29

    def test_expiring_soon_status(self, token_mgr: TokenManager) -> None:
        soon = (datetime.now(ZoneInfo("Asia/Tokyo")) + timedelta(days=3)).isoformat()
        token_mgr.sm.save_json(TokenManager.TOKEN_STATE_FILE, {
            "access_token": "t",
            "refreshed_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            "expires_at": soon,
        })
        status = token_mgr.get_token_status()
        assert status["status"] == "expiring_soon"
        assert status["needs_refresh"] is True

    def test_expired_status(self, token_mgr: TokenManager) -> None:
        past = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=1)).isoformat()
        token_mgr.sm.save_json(TokenManager.TOKEN_STATE_FILE, {
            "access_token": "t",
            "refreshed_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            "expires_at": past,
        })
        status = token_mgr.get_token_status()
        assert status["status"] == "expired"


class TestInitialiseTokenState:
    """Tests for TokenManager.initialise_token_state."""

    def test_creates_state_from_env(self, token_mgr: TokenManager) -> None:
        with patch.dict("os.environ", {"THREADS_ACCESS_TOKEN": "init_token"}):
            token_mgr.initialise_token_state()

        state = token_mgr.sm.load_json(TokenManager.TOKEN_STATE_FILE)
        assert state["access_token"] == "init_token"
        assert state["expires_at"] is not None

        # Verify expiry is approximately 60 days from now (both sides are JST-aware)
        expires = datetime.fromisoformat(state["expires_at"])
        delta = expires - datetime.now(ZoneInfo("Asia/Tokyo"))
        assert 59 <= delta.days <= 60


class TestEnsurePrLabel:
    """Tests for PosterAgent._ensure_pr_label (imported via poster)."""

    def _ensure(self, text: str) -> str:
        from agents.poster import PosterAgent
        return PosterAgent._ensure_pr_label(text)

    def test_adds_pr_when_missing(self) -> None:
        result = self._ensure("おすすめの化粧水はこちら！")
        assert result.startswith("PR\n")

    def test_preserves_existing_pr_label(self) -> None:
        result = self._ensure("PR\nおすすめの化粧水はこちら！")
        assert result == "PR\nおすすめの化粧水はこちら！"

    def test_preserves_kakko_pr(self) -> None:
        result = self._ensure("【PR】おすすめの化粧水はこちら！")
        assert result == "【PR】おすすめの化粧水はこちら！"

    def test_preserves_hashtag_pr(self) -> None:
        result = self._ensure("#PR おすすめの化粧水はこちら！")
        assert result == "#PR おすすめの化粧水はこちら！"

    def test_preserves_ad_label(self) -> None:
        result = self._ensure("広告\nおすすめの化粧水はこちら！")
        assert result == "広告\nおすすめの化粧水はこちら！"

    def test_preserves_kakko_ad(self) -> None:
        result = self._ensure("【広告】おすすめの化粧水はこちら！")
        assert result == "【広告】おすすめの化粧水はこちら！"

    def test_adds_pr_to_empty_string(self) -> None:
        result = self._ensure("")
        assert result.startswith("PR")
