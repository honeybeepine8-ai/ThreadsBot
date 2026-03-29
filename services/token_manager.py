"""OAuth token manager for Threads API long-lived tokens.

Threads long-lived tokens expire after 60 days. This module handles:
- Tracking token expiry dates
- Automatic refresh when approaching expiry
- Persisting refreshed tokens to data/state/token_state.json and .env
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from core.logger import get_logger
from core.state_manager import StateManager

load_dotenv()

logger = get_logger("token_manager")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

# Refresh when fewer than this many days remain
_REFRESH_THRESHOLD_DAYS = 7

# Threads token lifetime
_TOKEN_LIFETIME_DAYS = 60


class TokenManager:
    """Manage Threads API OAuth long-lived token lifecycle.

    The token state is persisted in ``data/state/token_state.json`` with the
    following schema::

        {
            "access_token": "...",
            "refreshed_at": "2026-03-22T10:00:00",
            "expires_at": "2026-05-21T10:00:00"
        }
    """

    TOKEN_STATE_FILE = "token_state.json"

    def __init__(self, state_manager: StateManager | None = None) -> None:
        self.sm = state_manager or StateManager()
        self.base_url = "https://graph.threads.net"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_valid_token(self) -> str:
        """Return a valid access token, refreshing if necessary.

        If the token is within ``_REFRESH_THRESHOLD_DAYS`` of expiry (or
        already expired), a refresh is attempted automatically.

        Returns:
            A valid access token string.

        Raises:
            TokenRefreshError: If the refresh attempt fails.
        """
        token_state = self.sm.load_json(self.TOKEN_STATE_FILE)

        current_token = token_state.get("access_token") or os.environ.get(
            "THREADS_ACCESS_TOKEN", ""
        )

        if not current_token:
            raise TokenRefreshError("No access token found in state or environment")

        # Check if we need to refresh
        expires_at_str = token_state.get("expires_at")

        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now(ZoneInfo("Asia/Tokyo"))
            # Ensure both sides are aware (handle legacy naive data)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            days_remaining = (expires_at - now).days

            if days_remaining > _REFRESH_THRESHOLD_DAYS:
                logger.debug(
                    "Token valid for %d more days (expires %s)",
                    days_remaining,
                    expires_at_str,
                )
                return current_token

            logger.info(
                "Token expires in %d days — refreshing now", days_remaining
            )
        else:
            # No expiry tracked — initialise state from current token
            logger.info("No token expiry tracked. Initialising token state.")
            self._save_token_state(current_token)
            return current_token

        # Attempt refresh
        new_token = self._refresh_token(current_token)
        self._save_token_state(new_token)
        self._update_env_file(new_token)

        return new_token

    def get_token_status(self) -> dict:
        """Return current token health information.

        Returns:
            A dict with ``days_remaining``, ``expires_at``, and ``needs_refresh``.
        """
        token_state = self.sm.load_json(self.TOKEN_STATE_FILE)
        expires_at_str = token_state.get("expires_at")

        if not expires_at_str:
            return {
                "days_remaining": None,
                "expires_at": None,
                "needs_refresh": True,
                "status": "unknown",
            }

        expires_at = datetime.fromisoformat(expires_at_str)
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        days_remaining = (expires_at - now).days

        if days_remaining <= 0:
            status = "expired"
        elif days_remaining <= _REFRESH_THRESHOLD_DAYS:
            status = "expiring_soon"
        else:
            status = "valid"

        return {
            "days_remaining": days_remaining,
            "expires_at": expires_at_str,
            "needs_refresh": days_remaining <= _REFRESH_THRESHOLD_DAYS,
            "status": status,
        }

    def initialise_token_state(self) -> None:
        """Set up initial token state from the environment variable.

        Call this once during setup to start tracking expiry.
        The initial expiry is assumed to be 60 days from now.
        """
        token = os.environ.get("THREADS_ACCESS_TOKEN", "")
        if not token:
            logger.warning("THREADS_ACCESS_TOKEN not set — cannot initialise")
            return

        self._save_token_state(token)
        logger.info("Token state initialised (assumed 60-day expiry from now)")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refresh_token(self, current_token: str) -> str:
        """Exchange the current long-lived token for a new one.

        Uses the Threads token refresh endpoint::

            GET /refresh_access_token
                ?grant_type=th_exchange_token
                &access_token={current_token}

        Args:
            current_token: The current (still valid) long-lived token.

        Returns:
            The new access token string.

        Raises:
            TokenRefreshError: If the API call fails.
        """
        url = f"{self.base_url}/refresh_access_token"
        params = {
            "grant_type": "th_exchange_token",
            "access_token": current_token,
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, params=params)
                response.raise_for_status()

            data = response.json()
            new_token = data.get("access_token")

            if not new_token:
                raise TokenRefreshError(
                    f"Refresh response missing access_token: {data}"
                )

            logger.info("Token refreshed successfully")
            return new_token

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = exc.response.text[:300]
            # Sanitize: never include tokens in logs or error messages
            if current_token and current_token in body:
                body = body.replace(current_token, "***TOKEN***")
            logger.error("Token refresh failed (HTTP %d): %s", status, body)
            raise TokenRefreshError(
                f"Token refresh failed (HTTP {status})"
            ) from exc
        except httpx.HTTPError as exc:
            # Sanitize: exception message may contain URL with token in query params
            err_msg = str(exc)
            if current_token and current_token in err_msg:
                err_msg = err_msg.replace(current_token, "***TOKEN***")
            logger.error("Token refresh network error: %s", err_msg)
            raise TokenRefreshError(f"Token refresh network error: {err_msg}") from exc

    def _save_token_state(self, token: str) -> None:
        """Persist token and computed expiry to state file."""
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        expires_at = now + timedelta(days=_TOKEN_LIFETIME_DAYS)

        state = {
            "access_token": token,
            "refreshed_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self.sm.save_json(self.TOKEN_STATE_FILE, state)
        logger.debug("Token state saved (expires %s)", expires_at.isoformat())

    @staticmethod
    def _update_env_file(new_token: str) -> None:
        """Update THREADS_ACCESS_TOKEN in the .env file atomically.

        Uses a temp-file + replace pattern to avoid partial writes that
        could corrupt the .env file if the process is interrupted.
        """
        if not _ENV_PATH.exists():
            logger.warning(".env file not found at %s — skipping update", _ENV_PATH)
            return

        content = _ENV_PATH.read_text(encoding="utf-8")

        pattern = r"^THREADS_ACCESS_TOKEN=.*$"
        replacement = f"THREADS_ACCESS_TOKEN={new_token}"

        new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)

        if count == 0:
            logger.warning("THREADS_ACCESS_TOKEN not found in .env — skipping update")
            return

        # Atomic write via temp file + os.replace
        import tempfile

        fd, tmp_path = tempfile.mkstemp(
            dir=str(_ENV_PATH.parent), suffix=".tmp", prefix=".env"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp_path, str(_ENV_PATH))
        except OSError:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        logger.info(".env file updated with new token")

        # Also update the runtime environment
        os.environ["THREADS_ACCESS_TOKEN"] = new_token


class TokenRefreshError(Exception):
    """Raised when a token refresh attempt fails."""
