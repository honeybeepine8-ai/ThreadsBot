"""Quality gate: NG-word filtering, similarity check, and pattern rotation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logger import get_logger

_NG_WORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "ng_words.txt"
_BRAND_NAMES_PATH = Path(__file__).resolve().parent.parent / "config" / "brand_names.txt"
_LEVEL_HEADER_RE = re.compile(r"#.*Level\s+(\d)")

logger = get_logger("quality_gate")


@dataclass
class _NgRule:
    """A single NG-word rule (plain substring or compiled regex)."""
    raw: str
    level: int  # 1, 2, or 3
    pattern: re.Pattern[str] | None = None

    def matches(self, content: str) -> bool:
        if self.pattern:
            return bool(self.pattern.search(content))
        return self.raw in content


@dataclass
class NgCheckResult:
    """Result of NG-word checking, categorised by level."""
    level1_hits: list[str] = field(default_factory=list)
    level2_hits: list[str] = field(default_factory=list)
    level3_hits: list[str] = field(default_factory=list)

    @property
    def has_block(self) -> bool:
        return bool(self.level1_hits)

    @property
    def has_warning(self) -> bool:
        return bool(self.level2_hits)

    @property
    def all_hits(self) -> list[str]:
        return self.level1_hits + self.level2_hits + self.level3_hits


class QualityGate:
    """Content quality checks executed before a post enters the queue."""

    # Short ASCII-only brand names need word-boundary matching to avoid
    # false positives (e.g. "VT" in "invite", "DHC" in "DHCP").
    _SHORT_ASCII_RE = re.compile(r"^[A-Za-z0-9.+\-]+$")
    _SHORT_ASCII_MAX_LEN = 4

    def __init__(self) -> None:
        self._ng_rules: list[_NgRule] | None = None
        self._brand_rules: list[tuple[str, re.Pattern[str] | None]] | None = None
        self._max_similarity: float | None = None
        self._max_hook_similarity: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_ng_words(self, content: str) -> list[str]:
        """Return all NG words found in *content* (flat list for backward compat).

        See :meth:`check_ng_words_v2` for level-categorised results.
        """
        return self.check_ng_words_v2(content).all_hits

    def check_ng_words_v2(self, content: str) -> NgCheckResult:
        """Return NG-word matches categorised by level.

        NG rules are loaded (and cached) from ``config/ng_words.txt``.
        Lines starting with ``/`` and ending with ``/`` are compiled as
        regular expressions.  All other non-blank, non-comment lines are
        plain substring matches.

        """
        rules = self._load_ng_rules()
        result = NgCheckResult()
        for rule in rules:
            if rule.matches(content):
                hit_label = rule.raw
                if rule.level == 1:
                    result.level1_hits.append(hit_label)
                elif rule.level == 2:
                    result.level2_hits.append(hit_label)
                else:
                    result.level3_hits.append(hit_label)

        return result

    def check_brand_names(self, content: str) -> list[str]:
        """Return brand/product names found in *content*.

        Brand names are loaded (and cached) from ``config/brand_names.txt``.
        Short ASCII-only names (<=4 chars) use word-boundary regex to
        avoid false positives; all others use substring matching.

        Args:
            content: The candidate post body.

        Returns:
            A list of matched brand name strings.
        """
        rules = self._load_brand_rules()
        hits: list[str] = []
        for name, pattern in rules:
            if pattern:
                if pattern.search(content):
                    hits.append(name)
            elif name in content:
                hits.append(name)
        if hits:
            logger.warning("Brand names detected: %s", hits)
        return hits

    def check_similarity(
        self, content: str, history_contents: list[str]
    ) -> float:
        """Compute the maximum cosine similarity between *content* and *history_contents*.

        Uses TF-IDF vectorisation from scikit-learn.

        Args:
            content: The candidate post body.
            history_contents: Previously published post bodies.

        Returns:
            The highest cosine similarity score (0.0 -- 1.0).
            Returns ``0.0`` when *history_contents* is empty.
        """
        if not history_contents:
            return 0.0

        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        corpus = history_contents + [content]
        # char_wb + ngram(2,4): 日本語テキストを文字N-gramでトークン化
        # （MeCab等の外部トークナイザ不要）
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
        tfidf_matrix = vectorizer.fit_transform(corpus)

        # Compare the last vector (candidate) against all previous ones
        candidate_vec = tfidf_matrix[-1]
        history_vecs = tfidf_matrix[:-1]
        similarities = cosine_similarity(candidate_vec, history_vecs)
        max_score: float = float(similarities.max())
        return max_score

    def check_hook_similarity(
        self, content: str, history_contents: list[str]
    ) -> float:
        """Compute similarity between the first line (hook) of *content* and hooks in *history_contents*.

        Extracts the first non-empty line from each text and compares
        using the same TF-IDF approach as :meth:`check_similarity`.

        Returns:
            The highest cosine similarity score (0.0 -- 1.0) among hooks.
            Returns ``0.0`` when *history_contents* is empty.
        """
        hook = self._extract_hook(content)
        if not hook:
            return 0.0

        history_hooks = [
            h for h in (self._extract_hook(c) for c in history_contents) if h
        ]
        if not history_hooks:
            return 0.0

        return self.check_similarity(hook, history_hooks)

    @staticmethod
    def _extract_hook(content: str) -> str:
        """Return the first non-empty line of *content*."""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return ""

    def check_pattern_rotation(
        self,
        pattern: str,
        recent_patterns: list[str],
        block_count: int = 3,
    ) -> bool:
        """Check whether *pattern* has been used in the most recent *block_count* posts.

        Args:
            pattern: The pattern label for the candidate post.
            recent_patterns: Ordered list of recently used pattern labels
                (most-recent first).
            block_count: How many recent patterns to compare against.

        Returns:
            ``True`` if the pattern is safe to use (NOT in recent history).
            ``False`` if the pattern was used within the last *block_count* posts.
        """
        recent_window = recent_patterns[:block_count]
        return pattern not in recent_window

    def check_debate_ng(
        self,
        content: str,
        ng_statements: list[str],
    ) -> dict[str, Any]:
        """Check content against debate-specific NG statements.

        Used for *yellow* safety-level debate themes to ensure the generated
        post does not contain any forbidden expressions.

        Args:
            content: The candidate post body.
            ng_statements: List of forbidden expressions for this debate theme.

        Returns:
            A dict with ``passed`` (bool) and ``reason`` (str) keys.
        """
        for stmt in ng_statements:
            if stmt in content:
                logger.warning("Debate NG statement found: '%s'", stmt)
                return {"passed": False, "reason": f"debate_ng: '{stmt}'"}
        return {"passed": True, "reason": "ok"}

    def validate(
        self,
        content: str,
        pattern: str,
        post_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run all quality checks and return a combined result.

        Checks are executed in order:

        1. NG-word scan
        2. Cosine-similarity check against recent posts
        3. Pattern-rotation check

        Args:
            content: The candidate post body.
            pattern: The pattern label for the candidate post.
            post_history: List of previous post dicts, each expected to
                contain ``"content"`` and ``"pattern"`` keys.

        Returns:
            A dict with keys ``passed``, ``reason``, ``similarity_score``,
            and ``ng_words``.
        """
        result: dict[str, Any] = {
            "passed": True,
            "reason": "ok",
            "similarity_score": 0.0,
            "hook_similarity_score": 0.0,
            "ng_words": [],
            "ng_check": None,
        }

        # 1. NG words (level-aware)
        ng_result = self.check_ng_words_v2(content)
        result["ng_check"] = {
            "level1_hits": ng_result.level1_hits,
            "level2_hits": ng_result.level2_hits,
            "level3_hits": ng_result.level3_hits,
        }
        result["ng_words"] = ng_result.all_hits
        if ng_result.has_block:
            result["passed"] = False
            result["reason"] = f"ng_level1_block: {ng_result.level1_hits}"
            logger.warning("NG Level 1 (block): %s", ng_result.level1_hits)
            return result
        if ng_result.has_warning:
            result["review_required"] = True
            logger.info("NG Level 2 (warning): %s", ng_result.level2_hits)

        # 1.5. Brand name filter (Layer 4 — Level 2 warning)
        brand_hits = self.check_brand_names(content)
        if brand_hits:
            result["ng_check"]["brand_hits"] = brand_hits
            result["review_required"] = True
            logger.info("Brand names found (review required): %s", brand_hits)

        # 2. Similarity
        history_contents = [
            p["content"] for p in post_history if p.get("content")
        ]
        if history_contents:
            score = self.check_similarity(content, history_contents)
            result["similarity_score"] = score
            # Threshold is loaded lazily to avoid circular imports at module level
            max_sim = self._get_max_similarity()
            if score >= max_sim:
                result["passed"] = False
                result["reason"] = (
                    f"similarity_too_high: {score:.3f} >= {max_sim}"
                )
                logger.warning(
                    "Similarity too high: %.3f (threshold %.2f)", score, max_sim
                )
                return result

        # 2.5. Hook (first line) similarity
        if history_contents:
            hook_score = self.check_hook_similarity(content, history_contents)
            result["hook_similarity_score"] = hook_score
            max_hook_sim = self._get_max_hook_similarity()
            if hook_score >= max_hook_sim:
                result["passed"] = False
                result["reason"] = (
                    f"hook_similarity_too_high: {hook_score:.3f} >= {max_hook_sim}"
                )
                logger.warning(
                    "Hook similarity too high: %.3f (threshold %.2f)",
                    hook_score,
                    max_hook_sim,
                )
                return result

        # 3. Pattern rotation
        recent_patterns = [
            p["pattern"] for p in post_history if p.get("pattern")
        ]
        if not self.check_pattern_rotation(pattern, recent_patterns):
            result["passed"] = False
            result["reason"] = f"pattern_repeated: {pattern}"
            logger.warning("Pattern repeated in recent posts: %s", pattern)
            return result

        logger.debug(
            "Quality gate passed (similarity=%.3f)", result["similarity_score"]
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_brand_rules(self) -> list[tuple[str, re.Pattern[str] | None]]:
        """Load and cache brand name rules from the config file.

        Reads ``config/brand_names.txt``, skipping blank lines and
        comment lines (starting with ``#``).

        Short ASCII-only names (<=4 chars like ``VT``, ``DHC``, ``YSL``)
        get a word-boundary regex to prevent false positives such as
        ``"VT"`` matching ``"invite"``.
        """
        if self._brand_rules is not None:
            return self._brand_rules

        rules: list[tuple[str, re.Pattern[str] | None]] = []
        try:
            with open(_BRAND_NAMES_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    # Short ASCII names → word-boundary regex
                    if (
                        len(stripped) <= self._SHORT_ASCII_MAX_LEN
                        and self._SHORT_ASCII_RE.match(stripped)
                    ):
                        # Use ASCII-only lookaround instead of \b to avoid
                        # false negatives with CJK characters (e.g. "DHCの化粧水").
                        pattern = re.compile(
                            r"(?<![A-Za-z0-9])" + re.escape(stripped) + r"(?![A-Za-z0-9])"
                        )
                        rules.append((stripped, pattern))
                    else:
                        rules.append((stripped, None))
        except FileNotFoundError:
            logger.error("Brand names file not found: %s", _BRAND_NAMES_PATH)

        self._brand_rules = rules
        logger.debug("Loaded %d brand name rules", len(rules))
        return rules

    def _load_ng_rules(self) -> list[_NgRule]:
        """Load and cache NG rules from the config file.

        Parses ``# Level N:`` section headers to assign levels.
        Lines matching ``/pattern/`` are compiled as regex.
        """
        if self._ng_rules is not None:
            return self._ng_rules

        rules: list[_NgRule] = []
        current_level = 1

        try:
            with open(_NG_WORDS_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("#"):
                        m = _LEVEL_HEADER_RE.search(stripped)
                        if m:
                            current_level = int(m.group(1))
                        continue

                    # Regex pattern: /pattern/
                    if stripped.startswith("/") and stripped.endswith("/") and len(stripped) > 2:
                        regex_body = stripped[1:-1]
                        try:
                            compiled = re.compile(regex_body)
                            rules.append(_NgRule(raw=stripped, level=current_level, pattern=compiled))
                        except re.error as exc:
                            logger.warning("Invalid regex in ng_words.txt: %s (%s)", stripped, exc)
                        continue

                    rules.append(_NgRule(raw=stripped, level=current_level))

        except FileNotFoundError:
            logger.error("NG words file not found: %s", _NG_WORDS_PATH)

        self._ng_rules = rules
        logger.debug("Loaded %d NG rules (regex + plain)", len(rules))
        return rules

    def _load_safety_config(self) -> dict[str, Any]:
        """Load and return the ``safety`` section from settings.yaml."""
        import yaml

        config_path = _NG_WORDS_PATH.parent / "settings.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg["safety"]

    def _get_max_similarity(self) -> float:
        """Return ``max_similarity_score`` from settings.yaml (cached)."""
        if self._max_similarity is not None:
            return self._max_similarity
        self._max_similarity = float(
            self._load_safety_config()["max_similarity_score"]
        )
        return self._max_similarity

    def _get_max_hook_similarity(self) -> float:
        """Return ``max_hook_similarity_score`` from settings.yaml (cached)."""
        if self._max_hook_similarity is not None:
            return self._max_hook_similarity
        self._max_hook_similarity = float(
            self._load_safety_config()["max_hook_similarity_score"]
        )
        return self._max_hook_similarity
