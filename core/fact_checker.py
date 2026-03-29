"""Fact-checking layer for Writer-generated content.

Two independent checks:

1. **Pharma law compliance** (yakukinhou) -- uses Claude to judge whether
   the post text stays within the 56 permitted cosmetic claims.
   Returns ``safe`` / ``borderline`` / ``violation``.

2. **Numerical claim verification** -- extracts numbers + unit patterns
   from the post and checks whether they appear in
   ``knowledge/domain_knowledge.md``.  Unknown claims are flagged with
   ``fact_check_required=true`` so a human reviewer can verify them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.logger import get_logger
from core.safety import ComplianceChecker
from services.claude_client import ClaudeClient

logger = get_logger("fact_checker")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOMAIN_KNOWLEDGE_PATH = _PROJECT_ROOT / "knowledge" / "domain_knowledge.md"

# Regex patterns for numerical claims in skincare context
# Matches patterns like: 50%, 5%, 0.02mm, 28日, 6L, 500枚, 56項目 etc.
_NUMBER_CLAIM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(%|mg|mL|g|mm|日|週間|時間|分|秒|倍|種類|本|枚|項目|個|年|ヶ月|回|段階|層|円)"
)

# Pharma-law violation keywords for fast local pre-screening.
# If none of these appear, the post is almost certainly safe and we
# can skip the expensive Claude API call.
_PHARMA_RISK_KEYWORDS = [
    "治る", "治す", "治し", "治った",
    "効く", "効果がある", "効能",
    "消える", "消す", "消した",
    "シミが消", "シワがなく", "シワが消",
    "若返", "アンチエイジング",
    "コラーゲンを増",
    "ターンオーバーを正常化",
    "肌が生まれ変わ",
    "浸透する", "浸透した", "浸透して",
    "再生", "改善する", "改善した",
    "No.1", "最安値", "100%の人",
]


def _load_text(path: Path) -> str:
    """Load a text file, returning empty string on failure."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Failed to load %s", path)
        return ""


class FactChecker:
    """Run pharma-law and numerical-claim checks on post content."""

    def __init__(self, claude_client: ClaudeClient | None = None) -> None:
        self._claude = claude_client or ClaudeClient()
        self._compliance = ComplianceChecker(claude_client=self._claude)
        self._domain_knowledge = _load_text(_DOMAIN_KNOWLEDGE_PATH)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_pharma_law(self, content: str) -> dict[str, Any]:
        """Check whether *content* complies with pharmaceutical law.

        A fast local pre-screen runs first: if the content contains no
        pharma-risk keywords, the post is considered safe without calling
        the Claude API.  Only posts containing risky language are sent
        to :class:`ComplianceChecker` for detailed judgement.

        Returns:
            A dict with keys:
            - ``result``: ``"safe"`` | ``"borderline"`` | ``"violation"``
            - ``reason``: explanation string (empty for ``safe``)
        """
        # Fast local pre-screen — skip Claude if no risk keywords found
        if not any(kw in content for kw in _PHARMA_RISK_KEYWORDS):
            logger.debug("Pharma pre-screen: no risk keywords found — safe.")
            return {"result": "safe", "reason": ""}

        logger.info("Pharma risk keyword detected — delegating to ComplianceChecker.")
        cc_result = self._compliance.check(content)

        result = cc_result["verdict"]
        reason = cc_result.get("reason", "")
        logger.info(
            "Pharma law check: %s%s",
            result,
            f" ({reason})" if reason else "",
        )
        return {"result": result, "reason": reason}

    def check_numerical_claims(self, content: str) -> dict[str, Any]:
        """Extract numerical claims and verify against domain knowledge.

        Returns:
            A dict with keys:
            - ``fact_check_required``: bool
            - ``claims_found``: list of extracted claim strings
            - ``unknown_claims``: list of claims not found in domain knowledge
        """
        matches = _NUMBER_CLAIM_RE.findall(content)
        if not matches:
            return {
                "fact_check_required": False,
                "claims_found": [],
                "unknown_claims": [],
            }

        claims_found: list[str] = []
        unknown_claims: list[str] = []

        for number, unit in matches:
            claim_str = f"{number}{unit}"
            claims_found.append(claim_str)

            if not self._claim_in_knowledge(number, unit, claim_str):
                unknown_claims.append(claim_str)

        fact_check_required = len(unknown_claims) > 0

        if fact_check_required:
            logger.info(
                "Fact check required: %d unknown claim(s): %s",
                len(unknown_claims),
                ", ".join(unknown_claims),
            )
        else:
            logger.debug(
                "All %d numerical claims verified in domain knowledge.",
                len(claims_found),
            )

        return {
            "fact_check_required": fact_check_required,
            "claims_found": claims_found,
            "unknown_claims": unknown_claims,
        }

    def run_all_checks(self, content: str) -> dict[str, Any]:
        """Run both pharma-law and numerical-claim checks.

        Returns:
            A combined dict with:
            - ``pharma_law``: result of check_pharma_law()
            - ``numerical``: result of check_numerical_claims()
            - ``fact_check_required``: bool
            - ``review_required``: bool (True if borderline or unknown claims)
            - ``blocked``: bool (True if violation)
            - ``block_reason``: str (reason if blocked)
        """
        pharma = self.check_pharma_law(content)
        numerical = self.check_numerical_claims(content)

        blocked = pharma["result"] == "violation"
        review_required = (
            pharma["result"] == "borderline"
            or numerical["fact_check_required"]
        )

        return {
            "pharma_law": pharma,
            "numerical": numerical,
            "fact_check_required": numerical["fact_check_required"],
            "review_required": review_required,
            "blocked": blocked,
            "block_reason": pharma["reason"] if blocked else "",
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _claim_in_knowledge(
        self, number: str, unit: str, claim_str: str,
    ) -> bool:
        """Check whether a numerical claim appears in domain knowledge."""
        if not self._domain_knowledge:
            return False

        # Direct string match (most reliable)
        if claim_str in self._domain_knowledge:
            return True

        # Try with space between number and unit
        if f"{number} {unit}" in self._domain_knowledge:
            return True

        # For percentages, also try "約N%" pattern
        if unit == "%":
            if f"約{number}%" in self._domain_knowledge:
                return True
            if f"約{number} %" in self._domain_knowledge:
                return True

        # Common known numbers that are fine without explicit listing
        # (e.g., "3つ", "5つ" are just list counts, not claims)
        trivial_units = {"つ", "個", "本", "枚", "回", "段階", "種類", "件", "項目"}
        if unit in trivial_units and float(number) <= 10:
            return True

        return False
