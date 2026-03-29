"""Tests for core.scheduler CLI argument parsing and account dispatch."""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestParseArgs:

    def test_default_account_single_agent(self):
        from core.scheduler import _parse_args
        with patch("sys.argv", ["scheduler", "poster"]):
            account_id, agent_name = _parse_args()
        assert account_id == "default"
        assert agent_name == "poster"

    def test_explicit_account(self):
        from core.scheduler import _parse_args
        with patch("sys.argv", ["scheduler", "--account", "riku", "writer"]):
            account_id, agent_name = _parse_args()
        assert account_id == "riku"
        assert agent_name == "writer"

    def test_account_all(self):
        from core.scheduler import _parse_args
        with patch("sys.argv", ["scheduler", "--account", "all", "all"]):
            account_id, agent_name = _parse_args()
        assert account_id == "all"
        assert agent_name == "all"

    def test_case_normalization(self):
        from core.scheduler import _parse_args
        with patch("sys.argv", ["scheduler", "--account", "Riku", "Writer"]):
            account_id, agent_name = _parse_args()
        assert account_id == "riku"
        assert agent_name == "writer"

    def test_help_exits(self):
        from core.scheduler import _parse_args
        with patch("sys.argv", ["scheduler", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                _parse_args()
            assert exc_info.value.code == 0

    def test_no_args_exits(self):
        from core.scheduler import _parse_args
        with patch("sys.argv", ["scheduler"]):
            with pytest.raises(SystemExit) as exc_info:
                _parse_args()
            assert exc_info.value.code == 0

    def test_account_missing_args_exits(self):
        from core.scheduler import _parse_args
        with patch("sys.argv", ["scheduler", "--account", "riku"]):
            with pytest.raises(SystemExit) as exc_info:
                _parse_args()
            assert exc_info.value.code == 1


class TestListAccounts:

    def test_finds_riku_and_hina(self):
        from core.scheduler import _list_accounts
        accounts = _list_accounts()
        assert "riku" in accounts
        assert "hina" in accounts

    def test_excludes_template(self):
        from core.scheduler import _list_accounts
        accounts = _list_accounts()
        assert "_template" not in accounts
