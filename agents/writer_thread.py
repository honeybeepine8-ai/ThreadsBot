"""WriterThreadMixin — thread (multi-post) generation logic."""

from __future__ import annotations

import random
import re
from typing import Any

import datetime


class WriterThreadMixin:
    """Mixin providing thread generation methods for WriterAgent."""

    _THREAD_SEPARATOR = "---THREAD_BREAK---"

    def _generate_thread_post(
        self,
        research_item: dict[str, Any],
        now: datetime.datetime,
        time_slot: str,
        is_pr_slot: bool,
        debate: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Generate a thread-format post (multiple self-reply posts).

        Returns a queue item with ``thread_posts`` containing the follow-up
        post texts, or ``None`` if generation fails.
        """
        from agents.writer_constants import _CATEGORY_HASHTAGS, _PROFILE_CTA_TEMPLATES

        history_data = self.state.load_json("post_history.json")
        recent_posts = history_data.get("posts", [])[-self.similarity_compare_count:]

        num_posts = random.randint(self.thread_min_posts, self.thread_max_posts)

        for attempt in range(1, self.max_generation_attempts + 1):
            self.logger.debug(
                "Thread generation attempt %d/%d for '%s' (%d posts)",
                attempt, self.max_generation_attempts,
                research_item.get("id", "?"), num_posts,
            )

            prompt = self._build_thread_prompt(
                research_item, num_posts, debate=debate,
            )
            raw = self.claude_client.generate_post(
                prompt=prompt, system_prompt=self.system_prompt,
            )

            # Parse the thread parts
            parts = self._parse_thread_response(raw, num_posts)
            if not parts or len(parts) < 2:
                self.logger.debug("Thread parse failed (got %d parts). Retrying.",
                                  len(parts) if parts else 0)
                continue

            # Validate first post (main) through quality gate
            main_content = parts[0]
            if (len(main_content) < self.min_char_count
                    or len(main_content) > self.max_char_count):
                continue

            gate_result = self.quality_gate.validate(
                content=main_content, pattern="ツリー展開型",
                post_history=recent_posts,
            )
            if not gate_result["passed"]:
                self.logger.debug("Thread quality gate rejected: %s", gate_result["reason"])
                continue

            # Validate follow-up posts (basic length check)
            follow_ups = parts[1:]
            valid = True
            for fp in follow_ups:
                if len(fp) < self.min_char_count or len(fp) > self.max_char_count:
                    valid = False
                    break
                ng = self.quality_gate.check_ng_words(fp)
                if ng:
                    self.logger.debug("Thread follow-up NG words: %s", ng)
                    valid = False
                    break
            if not valid:
                continue

            # Debate NG check (all debate themes)
            if debate:
                ng_stmts = debate.get("ng_statements", [])
                if ng_stmts:
                    all_text = "\n".join(parts)
                    debate_check = self.quality_gate.check_debate_ng(all_text, ng_stmts)
                    if not debate_check["passed"]:
                        continue

            # Quality score on the first post
            quality_eval = self._evaluate_quality_score(main_content)
            avg_score = quality_eval.get("average", 0.0)
            if avg_score < self.min_quality_score:
                continue

            # Append CTA to last post (not first)
            if not is_pr_slot and random.random() < self.profile_cta_rate:
                cta = random.choice(_PROFILE_CTA_TEMPLATES)
                last_post = follow_ups[-1]
                if len(last_post) + len(cta) <= self.max_char_count:
                    follow_ups[-1] = last_post + cta

            # Build queue item
            date_str = now.strftime("%Y%m%d")
            queue_data = self.state.load_json("post_queue.json")
            seq = len(queue_data.get("queue", [])) + 1
            category = research_item.get("category", "skincare_knowledge")
            hashtag = _CATEGORY_HASHTAGS.get(category, "#スキンケア")

            return {
                "id": f"q_{date_str}_{seq:03d}",
                "research_id": research_item["id"],
                "content": main_content,
                "thread_posts": follow_ups,
                "hashtag": hashtag,
                "pattern": "ツリー展開型",
                "quality_score": round(avg_score, 1),
                "similarity_score": round(gate_result.get("similarity_score", 0.0), 2),
                "category": category,
                "scheduled_at": time_slot,
                "created_at": now.isoformat(),
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": None,
                "profile_cta_included": False,
                "cta_pr_label": False,
                "debate_id": debate["id"] if debate else None,
                "debate_title": debate["title"] if debate else None,
            }

        return None

    def _build_thread_prompt(
        self,
        research_item: dict[str, Any],
        num_posts: int,
        *,
        debate: dict[str, Any] | None = None,
    ) -> str:
        """Build a prompt that instructs Claude to generate a multi-post thread."""
        from agents.writer_constants import _CATEGORY_HASHTAGS

        # Tone rules
        style = self.tone_config.get("style", {})
        tone_rules = "\n".join(f"- {r}" for r in style.get("rules", []))
        use_endings = "、".join(style.get("use_endings", []))
        persona_cfg = self.tone_config.get("persona", {})

        parts: list[str] = [
            "## ネタ情報",
            f"- トピック: {research_item.get('topic', '')}",
            f"- 要約: {research_item.get('summary', '')}",
            f"- カテゴリ: {research_item.get('category', '')}",
            "",
            "## 投稿形式: スレッド（自己リプライ連結）",
            f"以下の形式で{num_posts}つのポストに分けてスレッドを作成してください。",
            "",
            "### 構成ルール",
            "- 1ポスト目: フック（読者のスクロールを止める）+ 導入",
            "- 2ポスト目: 本題の解説（具体的な成分知識・手順・根拠）",
        ]

        if num_posts >= 3:
            parts.append("- 3ポスト目: まとめ + シグネチャー語尾")

        parts.extend([
            "",
            f"### 各ポストは「{self._THREAD_SEPARATOR}」で区切ってください",
            "（ポスト間に区切り文字を入れる）",
            "",
            "## 口調ルール",
            f"- 一人称: {style.get('first_person') or '使わない（主語なし or 対象を主語にする）'}",
            f"- トーン: {style.get('tone', '')}",
            f"- 使ってよい語尾: {use_endings}",
            tone_rules,
            "",
            "## 制約",
            f"- 各ポスト{self.max_char_count}文字以内",
            f"- 各ポスト最低{self.min_char_count}文字",
            "- 絵文字は0〜2個まで",
            "- 漢字率30%以下",
            "- NGワード禁止",
            "- 1ポスト目のフックで読者のスクロールを止めること",
            "",
            "## トピックタグ（必須）",
            f"- 1ポスト目の本文中に {_CATEGORY_HASHTAGS.get(research_item.get('category', ''), '#スキンケア')} を1つだけ自然に組み込むこと",
            "- 末尾にポツンと置くのはNG。文中や文末の流れの中に溶け込ませる",
            "",
            f"## ペルソナ: {persona_cfg.get('display_name', '')}",
            "",
        ])

        # Inject debate theme if present
        if debate:
            talking_pts = "\n".join(
                f"- {tp}" for tp in debate.get("talking_points", [])
            )
            ng_stmts = "\n".join(
                f"- {ns}" for ns in debate.get("ng_statements", [])
            )
            parts.extend([
                "## 論争テーマ（このテーマでスレッドを作成）",
                f"テーマ: {debate.get('title', '')}",
                f"立場: {debate.get('stance', '')}",
                "使える論点:",
                talking_pts,
                "",
                "## 禁止表現",
                ng_stmts,
                "",
            ])

        parts.extend([
            f"ポスト本文のみを出力してください。各ポストの間に「{self._THREAD_SEPARATOR}」を入れてください。",
            "余計な説明は不要です。",
        ])

        return "\n".join(parts)

    def _parse_thread_response(
        self, raw: str, expected_count: int
    ) -> list[str] | None:
        """Parse a Claude response into individual thread posts.

        Args:
            raw: The raw API response text.
            expected_count: The number of posts we asked for.

        Returns:
            A list of cleaned post texts, or None if parsing fails.
        """
        text = self._clean_content(raw)

        # Split by the separator
        if self._THREAD_SEPARATOR in text:
            parts = text.split(self._THREAD_SEPARATOR)
        else:
            # Fallback: try splitting by common patterns
            parts = re.split(r"\n---\n|\n\n---\n\n", text)

        # Clean each part
        cleaned = [p.strip() for p in parts if p.strip()]

        if len(cleaned) < 2:
            return None

        # Cap at expected count
        return cleaned[:expected_count]
