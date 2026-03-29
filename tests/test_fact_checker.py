"""Tests for core.fact_checker.FactChecker."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from core.fact_checker import FactChecker


class TestCheckPharmaLawPreScreen:
    """check_pharma_law should skip Claude when no risk keywords are found."""

    def test_safe_without_claude_call(self) -> None:
        mock_client = MagicMock()
        checker = FactChecker(claude_client=mock_client)
        result = checker.check_pharma_law("セラミドは保湿に大切な成分です")
        assert result["result"] == "safe", "No risk keywords → safe without Claude"
        assert result["reason"] == ""
        mock_client.prompt.assert_not_called()


class TestCheckPharmaLawDelegation:
    """check_pharma_law should delegate to ComplianceChecker for risky content."""

    def test_violation_delegated(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "violation",
            "reason": "「シミが消える」は化粧品の効能範囲外",
            "flagged_expressions": ["シミが消える"],
        })
        checker = FactChecker(claude_client=mock_client)
        result = checker.check_pharma_law("この美容液でシミが消える")
        assert result["result"] == "violation"
        assert "シミが消える" in result["reason"]
        mock_client.prompt.assert_called_once()

    def test_borderline_delegated(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "borderline",
            "reason": "解釈次第",
            "flagged_expressions": [],
        })
        checker = FactChecker(claude_client=mock_client)
        result = checker.check_pharma_law("浸透して肌が変わる")
        assert result["result"] == "borderline"

    def test_safe_delegated(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "safe",
            "reason": "",
            "flagged_expressions": [],
        })
        checker = FactChecker(claude_client=mock_client)
        # "改善する" is a risk keyword → triggers Claude call
        result = checker.check_pharma_law("肌荒れを防ぐケアで改善する余地がある")
        assert result["result"] == "safe"
        mock_client.prompt.assert_called_once()

    def test_api_failure_returns_borderline(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.side_effect = RuntimeError("CLI timeout")
        checker = FactChecker(claude_client=mock_client)
        result = checker.check_pharma_law("この成分は効く")
        assert result["result"] == "borderline", (
            "API failure should fallback to borderline, not safe"
        )


class TestCheckNumericalClaims:
    """check_numerical_claims should extract and verify number+unit patterns."""

    def test_no_claims(self) -> None:
        mock_client = MagicMock()
        checker = FactChecker(claude_client=mock_client)
        result = checker.check_numerical_claims("保湿は大切です")
        assert result["fact_check_required"] is False
        assert result["claims_found"] == []

    def test_trivial_numbers_pass(self) -> None:
        mock_client = MagicMock()
        checker = FactChecker(claude_client=mock_client)
        result = checker.check_numerical_claims("3種類の成分が大事")
        assert result["fact_check_required"] is False, (
            "Small counts with trivial units should pass"
        )

    def test_unknown_claim_flagged(self) -> None:
        mock_client = MagicMock()
        checker = FactChecker(claude_client=mock_client)
        result = checker.check_numerical_claims("ナイアシンアミド99%で皮脂が激減")
        assert result["fact_check_required"] is True
        assert "99%" in result["unknown_claims"]


class TestRunAllChecks:
    """run_all_checks should combine pharma and numerical results."""

    def test_violation_blocks(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "violation",
            "reason": "薬機法違反",
            "flagged_expressions": ["シミが消える"],
        })
        checker = FactChecker(claude_client=mock_client)
        result = checker.run_all_checks("この成分でシミが消える")
        assert result["blocked"] is True
        assert result["review_required"] is False, (
            "When blocked, review_required should be False (violation > borderline)"
        )
        assert result["block_reason"] == "薬機法違反"

    def test_borderline_requires_review(self) -> None:
        mock_client = MagicMock()
        mock_client.prompt.return_value = json.dumps({
            "verdict": "borderline",
            "reason": "グレーゾーン",
            "flagged_expressions": [],
        })
        checker = FactChecker(claude_client=mock_client)
        result = checker.run_all_checks("浸透して肌が変わるかも")
        assert result["blocked"] is False
        assert result["review_required"] is True

    def test_safe_no_flags(self) -> None:
        mock_client = MagicMock()
        checker = FactChecker(claude_client=mock_client)
        result = checker.run_all_checks("保湿は大切です")
        assert result["blocked"] is False
        assert result["review_required"] is False
        assert result["fact_check_required"] is False
