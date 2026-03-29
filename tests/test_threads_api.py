"""Tests for services.threads_api — pagination in get_post_replies()."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from services.threads_api import ThreadsAPIClient, RateLimitError


def _make_reply(reply_id: str) -> dict:
    return {
        "id": reply_id,
        "text": f"Reply {reply_id}",
        "username": "user1",
        "timestamp": "2026-03-29T12:00:00+09:00",
    }


@pytest.fixture()
def api_client():
    """Create a ThreadsAPIClient with mocked externals."""
    env = {
        "THREADS_ACCESS_TOKEN": "test_token",
        "THREADS_USER_ID": "test_user",
    }
    with patch.dict("os.environ", env):
        with patch("services.threads_api.TokenManager"):
            client = ThreadsAPIClient()
            yield client
            client.close()


# ====================================================================
# Single page
# ====================================================================


class TestGetPostRepliesSinglePage:

    def test_single_page_no_cursors(self, api_client) -> None:
        api_client._request = MagicMock(return_value={
            "data": [_make_reply("r1"), _make_reply("r2")],
            "paging": {},
        })

        result = api_client.get_post_replies("media_001")

        assert len(result) == 2
        assert result[0]["id"] == "r1"
        api_client._request.assert_called_once()

    def test_no_paging_key(self, api_client) -> None:
        api_client._request = MagicMock(return_value={
            "data": [_make_reply("r1")],
        })

        result = api_client.get_post_replies("media_001")

        assert len(result) == 1
        api_client._request.assert_called_once()


# ====================================================================
# Multi-page pagination
# ====================================================================


class TestGetPostRepliesMultiPage:

    def test_follows_cursor_across_pages(self, api_client) -> None:
        api_client._request = MagicMock(side_effect=[
            {
                "data": [_make_reply("r1"), _make_reply("r2")],
                "paging": {"cursors": {"after": "cursor_abc"}},
            },
            {
                "data": [_make_reply("r3")],
                "paging": {},
            },
        ])

        result = api_client.get_post_replies("media_001")

        assert len(result) == 3
        assert [r["id"] for r in result] == ["r1", "r2", "r3"]
        assert api_client._request.call_count == 2
        # Verify second call includes after cursor
        second_call_params = api_client._request.call_args_list[1][1].get(
            "params", api_client._request.call_args_list[1][0][2]
            if len(api_client._request.call_args_list[1][0]) > 2 else {}
        )
        # Check via kwargs
        calls = api_client._request.call_args_list
        assert "after" in str(calls[1])

    def test_max_pages_cap_stops_pagination(self, api_client) -> None:
        """Pagination stops at max_pages even if cursors keep coming."""
        api_client._request = MagicMock(return_value={
            "data": [_make_reply("r1")],
            "paging": {"cursors": {"after": "next_cursor"}},
        })

        result = api_client.get_post_replies("media_001", max_pages=3)

        assert len(result) == 3  # 1 reply per page × 3 pages
        assert api_client._request.call_count == 3


# ====================================================================
# Empty results
# ====================================================================


class TestGetPostRepliesEmpty:

    def test_empty_first_page(self, api_client) -> None:
        api_client._request = MagicMock(return_value={
            "data": [],
            "paging": {},
        })

        result = api_client.get_post_replies("media_001")

        assert result == []
        api_client._request.assert_called_once()

    def test_empty_data_stops_despite_cursor(self, api_client) -> None:
        api_client._request = MagicMock(side_effect=[
            {
                "data": [_make_reply("r1")],
                "paging": {"cursors": {"after": "cursor_abc"}},
            },
            {
                "data": [],
                "paging": {"cursors": {"after": "more_cursor"}},
            },
        ])

        result = api_client.get_post_replies("media_001")

        assert len(result) == 1
        assert api_client._request.call_count == 2


# ====================================================================
# Rate limit mid-pagination
# ====================================================================


class TestGetPostRepliesRateLimit:

    def test_returns_partial_results_on_rate_limit(self, api_client) -> None:
        api_client._request = MagicMock(side_effect=[
            {
                "data": [_make_reply("r1"), _make_reply("r2")],
                "paging": {"cursors": {"after": "cursor_abc"}},
            },
            RateLimitError("429 Too Many Requests"),
        ])

        result = api_client.get_post_replies("media_001")

        assert len(result) == 2  # Partial data from page 1
        assert api_client._request.call_count == 2
