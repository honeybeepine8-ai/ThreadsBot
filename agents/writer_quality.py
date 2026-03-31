"""WriterQualityMixin — quality evaluation and content cleaning."""

from __future__ import annotations

import re
from typing import Any


class WriterQualityMixin:
    """Mixin providing quality scoring and content post-processing for WriterAgent."""

    def _evaluate_quality_score(self, content: str) -> dict[str, Any]:
        """Evaluate a post's quality using the Claude evaluator model.

        The scoring criteria are derived from the quality-score rubric in
        ``knowledge/posting_rules.md``.

        Args:
            content: The generated post text.

        Returns:
            A dict with ``scores`` (per-dimension), ``average``, and
            ``feedback`` keys.
        """
        from agents.writer_constants import (
            _CONTENT_QUALITY_GROUP,
            _EXPRESSION_QUALITY_GROUP,
            _HOOK_QUALITY_GROUP,
        )

        criteria = (
            "以下の10項目を各10点満点で採点してください。\n\n"
            "【内容品質群】\n"
            "1. 有益性（usefulness）: 読者が今日から行動を変えられる具体的情報があるか\n"
            "2. 具体性（specificity）: 成分名+濃度+条件など数字や固有名詞があるか\n"
            "3. 感情移入しやすさ（empathy）: 「わかる！」と共感できるか\n"
            "4. 行動誘発力（call_to_action）: 読後に試す・保存する・コメントする気持ちになるか\n\n"
            "【表現品質群】\n"
            "5. 自然さ（naturalness）: bot臭くないか。人間が書いたように読めるか\n"
            "6. テンポ（tempo）: 一文が短く、リズムよく最後まで読めるか\n"
            "7. 体験語り感（experiential）: 「この人、実際に試したんだな」と感じるか\n"
            "8. 業者臭さのなさ（non_commercial）: 友達の話を聞いている感覚か\n\n"
            "【フック品質群】\n"
            "9. フックの強さ（hook_strength）: 1行目だけで読み続けたくなるか。「何これ？」感があるか\n"
            "10. ペルソナ一致度（persona_match）: 匿名成分オタク（@seibun_love）のキャラクターと合っているか\n\n"
            "ペルソナ: 匿名の成分オタク。架空の経歴は名乗らない。"
            "友達に教えるくらいのカジュアルさ。一人称は使わない or 最小限。\n\n"
            "JSON形式のみを返してください:\n"
            '{"scores": {"usefulness": <float>, "specificity": <float>, '
            '"empathy": <float>, "call_to_action": <float>, "naturalness": <float>, '
            '"tempo": <float>, "experiential": <float>, "non_commercial": <float>, '
            '"hook_strength": <float>, "persona_match": <float>}, '
            '"feedback": "<string>"}'
        )

        try:
            result = self.claude_client.evaluate_quality(content, criteria)
            scores: dict[str, float] = result.get("scores", {})
            if scores:
                content_scores = [v for k, v in scores.items() if k in _CONTENT_QUALITY_GROUP]
                expression_scores = [v for k, v in scores.items() if k in _EXPRESSION_QUALITY_GROUP]
                hook_scores = [v for k, v in scores.items() if k in _HOOK_QUALITY_GROUP]
                for group_name, group_scores, group_set in [
                    ("Content", content_scores, _CONTENT_QUALITY_GROUP),
                    ("Expression", expression_scores, _EXPRESSION_QUALITY_GROUP),
                    ("Hook", hook_scores, _HOOK_QUALITY_GROUP),
                ]:
                    if len(group_scores) < len(group_set):
                        self.logger.warning(
                            "%s quality group incomplete: got %d of %d scores.",
                            group_name, len(group_scores), len(group_set),
                        )
                content_avg = sum(content_scores) / len(content_scores) if content_scores else None
                expression_avg = sum(expression_scores) / len(expression_scores) if expression_scores else None
                hook_avg = sum(hook_scores) / len(hook_scores) if hook_scores else None
                # 3-group average: only include groups that have scores
                group_avgs = [g for g in [content_avg, expression_avg, hook_avg] if g is not None]
                final_avg = sum(group_avgs) / len(group_avgs) if group_avgs else 0.0
                result["content_quality_avg"] = round(content_avg, 1) if content_avg is not None else None
                result["expression_quality_avg"] = round(expression_avg, 1) if expression_avg is not None else None
                result["hook_quality_avg"] = round(hook_avg, 1) if hook_avg is not None else None
                result["average"] = round(final_avg, 1)
            return result
        except Exception as exc:
            self.logger.error("Quality evaluation failed: %s", exc, exc_info=True)
            return {"scores": {}, "average": 0.0, "feedback": f"Evaluation error: {exc}"}

    @staticmethod
    def _clean_content(raw: str) -> str:
        """Strip markdown fences, extra whitespace, and separator lines.

        If the response contains ``---本文---`` / ``---アフィリエイトコメント---``
        markers, only the main body portion is returned.

        Args:
            raw: The raw text returned from the Claude API.

        Returns:
            Cleaned post content.
        """
        text = raw.strip()

        # Remove markdown code fences if present
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

        # Extract body portion if section markers exist
        if "---本文---" in text:
            parts = text.split("---本文---", 1)
            if len(parts) > 1:
                body = parts[1]
                # Strip follow-up comment section
                if "---追いコメント---" in body:
                    body = body.split("---追いコメント---", 1)[0]
                # Strip affiliate comment section
                if "---アフィリエイトコメント---" in body:
                    body = body.split("---アフィリエイトコメント---", 1)[0]
                text = body

        return text.strip()

    @staticmethod
    def _extract_follow_up_comment(raw: str) -> str | None:
        """Extract the follow-up comment for コメント誘導型 posts.

        Args:
            raw: The raw text that may contain ``---追いコメント---`` markers.

        Returns:
            The follow-up comment text (≤100 chars), or ``None`` if not found.
        """
        marker = "---追いコメント---"
        if marker not in raw:
            return None

        parts = raw.split(marker, 1)
        if len(parts) < 2:
            return None

        comment = parts[1].strip()
        # Remove trailing markdown fences or affiliate section
        if "---アフィリエイトコメント---" in comment:
            comment = comment.split("---アフィリエイトコメント---", 1)[0].strip()
        comment = re.sub(r"\n?```$", "", comment).strip()
        return comment[:100] if comment else None

    @staticmethod
    def _extract_affiliate_comment(raw: str) -> str | None:
        """Extract the affiliate comment from a raw API response.

        Args:
            raw: The raw text that may contain
                ``---アフィリエイトコメント---`` markers.

        Returns:
            The affiliate comment text, or ``None`` if not found.
        """
        marker = "---アフィリエイトコメント---"
        if marker not in raw:
            return None

        parts = raw.split(marker, 1)
        if len(parts) < 2:
            return None

        comment = parts[1].strip()
        # Remove trailing markdown fences
        comment = re.sub(r"\n?```$", "", comment).strip()
        return comment if comment else None
