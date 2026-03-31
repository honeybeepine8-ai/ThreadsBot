"""Threads API client for creating, publishing, and managing posts."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

from core.logger import get_logger
from services.token_manager import TokenManager, TokenRefreshError

load_dotenv()

logger = get_logger("threads_api")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


def _load_threads_config() -> dict:
    """Load the ``threads_api`` section from *settings.yaml*."""
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("threads_api", {})


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ThreadsAPIError(Exception):
    """Base exception for Threads API errors."""


class RateLimitError(ThreadsAPIError):
    """Raised when the API returns HTTP 429 (Too Many Requests)."""


class AuthenticationError(ThreadsAPIError):
    """Raised when the API returns HTTP 401 or 403."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ThreadsAPIClient:
    """Synchronous client for the Threads (Meta) public API.

    Environment variables (loaded from ``.env``):
        - ``THREADS_ACCESS_TOKEN``
        - ``THREADS_USER_ID``
    """

    def __init__(self) -> None:
        self.user_id: str = os.environ["THREADS_USER_ID"]

        config = _load_threads_config()
        self.base_url: str = config.get("base_url", "https://graph.threads.net/v1.0")
        self.container_wait: int = config.get("media_container_wait_seconds", 30)

        self._token_manager = TokenManager()
        self._ensure_valid_token()

        self._client = httpx.Client(timeout=30.0)
        logger.info("ThreadsAPIClient initialised (user_id=%s)", self.user_id)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _ensure_valid_token(self) -> None:
        """Refresh the access token if it is near expiry."""
        try:
            self.access_token = self._token_manager.get_valid_token()
        except TokenRefreshError as exc:
            logger.warning("Token refresh failed, falling back to env: %s", exc)
            self.access_token = os.environ["THREADS_ACCESS_TOKEN"]

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def create_text_post(
        self,
        text: str,
        reply_to_id: str | None = None,
        quote_post_id: str | None = None,
    ) -> dict:
        """Create and publish a text post (optionally as a reply or quote).

        Args:
            text: The post body text.
            reply_to_id: If given, the post is created as a reply/comment
                to the specified media ID.
            quote_post_id: If given, the post is created as a quote repost
                of the specified media ID.

        Returns:
            ``{"id": "<media_id>", "success": True}``
        """
        # Step 1 – create media container
        params: dict[str, str] = {
            "media_type": "TEXT",
            "text": text,
        }
        if reply_to_id is not None:
            params["reply_to_id"] = reply_to_id
        if quote_post_id is not None:
            params["quote_post_id"] = quote_post_id

        container = self._request(
            "POST",
            f"/{self.user_id}/threads",
            params=params,
        )
        container_id: str = container["id"]
        logger.info("Media container created: %s", container_id)

        # Step 2 – wait for processing
        logger.debug("Waiting %d seconds for container processing …", self.container_wait)
        time.sleep(self.container_wait)

        # Step 3 – publish
        result = self._request(
            "POST",
            f"/{self.user_id}/threads_publish",
            params={"creation_id": container_id},
        )
        media_id: str = result["id"]
        logger.info("Post published: %s", media_id)

        return {"id": media_id, "success": True}

    def get_post_insights(self, media_id: str) -> dict:
        """Fetch engagement insights for a published post.

        Args:
            media_id: The Threads media ID.

        Returns:
            A dict with keys ``views``, ``likes``, ``replies``,
            ``reposts``, ``quotes`` (all ``int``).
        """
        raw = self._request(
            "GET",
            f"/{media_id}/insights",
            params={"metric": "views,likes,replies,reposts,quotes"},
        )

        insights: dict[str, int] = {
            "views": 0,
            "likes": 0,
            "replies": 0,
            "reposts": 0,
            "quotes": 0,
        }
        for entry in raw.get("data", []):
            name = entry.get("name")
            if name in insights:
                values = entry.get("values", [{}])
                insights[name] = int(values[0].get("value", 0)) if values else 0

        logger.debug("Insights for %s: %s", media_id, insights)
        return insights

    def get_post_replies(
        self,
        media_id: str,
        *,
        max_pages: int = 10,
    ) -> list[dict]:
        """Fetch replies (comments) on a published post.

        Automatically follows cursor-based pagination so that all replies
        are returned, not just the first page.

        Args:
            media_id: The Threads media ID of the parent post.
            max_pages: Safety cap on the number of pages fetched to
                prevent runaway requests (default 10).

        Returns:
            A list of reply dicts, each containing ``id``, ``text``,
            ``username``, and ``timestamp``.
        """
        all_replies: list[dict] = []
        params: dict[str, str] = {"fields": "id,text,username,timestamp"}
        pages_fetched = 0

        for pages_fetched in range(1, max_pages + 1):  # 1-indexed for logging
            try:
                raw = self._request(
                    "GET",
                    f"/{media_id}/replies",
                    params=params,
                )
            except RateLimitError:
                logger.warning(
                    "Rate limited during pagination for %s (page %d); "
                    "returning %d partial replies.",
                    media_id,
                    pages_fetched,
                    len(all_replies),
                )
                break
            data = raw.get("data", [])
            all_replies.extend(data)

            # Follow cursor-based pagination
            paging = raw.get("paging", {})
            after_cursor = paging.get("cursors", {}).get("after")
            if not after_cursor or not data:
                break
            params = {
                "fields": "id,text,username,timestamp",
                "after": after_cursor,
            }

        logger.debug(
            "Fetched %d replies for %s (%d page(s))",
            len(all_replies),
            media_id,
            pages_fetched,
        )
        return all_replies

    def get_user_profile(self) -> dict:
        """Fetch the authenticated user's profile.

        Returns:
            A dict containing ``id``, ``username``, and
            ``threads_biography``.
        """
        return self._request(
            "GET",
            "/me",
            params={"fields": "id,username,threads_biography"},
        )

    def delete_post(self, media_id: str) -> bool:
        """Delete a published post.

        Args:
            media_id: The Threads media ID to delete.

        Returns:
            ``True`` if the deletion succeeded.
        """
        result = self._request("DELETE", f"/{media_id}")
        success: bool = result.get("success", False)
        if success:
            logger.info("Post deleted: %s", media_id)
        else:
            logger.warning("Delete request returned unexpected payload: %s", result)
        return success

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Send an HTTP request to the Threads API.

        The access token is automatically appended to the query
        parameters.

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, ``"DELETE"``).
            endpoint: API path (e.g. ``"/me"``).
            **kwargs: Extra keyword arguments forwarded to
                :meth:`httpx.Client.request`.  ``params`` will be
                merged with the access-token parameter.

        Returns:
            Parsed JSON response body as a ``dict``.

        Raises:
            RateLimitError: On HTTP 429.
            AuthenticationError: On HTTP 401 or 403.
            ThreadsAPIError: On any other HTTP error.
        """
        url = f"{self.base_url}{endpoint}"

        # Merge access_token into params
        params: dict = kwargs.pop("params", {}) or {}
        params["access_token"] = self.access_token
        kwargs["params"] = params

        logger.debug("%s %s params=%s", method, url, {k: v for k, v in params.items() if k != "access_token"})

        try:
            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = exc.response.text[:500]
            # Sanitize: never log tokens in error bodies
            if self.access_token and self.access_token in body:
                body = body.replace(self.access_token, "***TOKEN***")
            logger.error(
                "Threads API error %d for %s %s: %s",
                status,
                method,
                endpoint,
                body,
            )

            if status == 429:
                raise RateLimitError(f"Rate limited (429): {body}") from exc
            if status in (401, 403):
                raise AuthenticationError(f"Authentication failed ({status}): {body}") from exc
            raise ThreadsAPIError(f"HTTP {status}: {body}") from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.error("Network error for %s %s: %s", method, endpoint, exc)
            raise ThreadsAPIError(f"Network error: {endpoint}: {exc}") from exc

        return response.json()
