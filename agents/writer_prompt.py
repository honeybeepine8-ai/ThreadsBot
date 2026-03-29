"""WriterPromptMixin — prompt construction logic for WriterAgent."""

from __future__ import annotations

from typing import Any


class WriterPromptMixin:
    """Mixin providing prompt-building methods for WriterAgent."""

    def _build_prompt(
        self,
        research_item: dict[str, Any],
        pattern: str,
        *,
        is_affiliate: bool = False,
        debate: dict[str, Any] | None = None,
    ) -> str:
        """Assemble the user prompt for the Claude API call.

        Args:
            research_item: The research pool item with ``topic`` and ``summary``.
            pattern: The selected posting pattern name.
            is_affiliate: Whether to include affiliate comment instructions.
            debate: Optional debate theme dict from debate_whitelist.yaml.

        Returns:
            A fully formatted prompt string.
        """
        from agents.writer_constants import _CATEGORY_HASHTAGS

        # Extract the pattern template from posting_rules.md
        pattern_template = self._extract_pattern_template(pattern)

        # Tone rules from tone.yaml
        style: dict[str, Any] = self.tone_config.get("style", {})
        tone_rules = "\n".join(f"- {r}" for r in style.get("rules", []))
        use_endings = "、".join(style.get("use_endings", []))
        avoid_endings = "、".join(style.get("avoid_endings", []))
        preferred_emoji = "、".join(style.get("preferred_emoji", []))
        avoid_emoji = "、".join(style.get("avoid_emoji", []))

        persona_cfg: dict[str, Any] = self.tone_config.get("persona", {})

        prompt_parts: list[str] = [
            "## ネタ情報",
            f"- トピック: {research_item.get('topic', '')}",
            f"- 要約: {research_item.get('summary', '')}",
            f"- カテゴリ: {research_item.get('category', '')}",
            "",
            "## 投稿パターン",
            f"パターン名: {pattern}",
            "",
            pattern_template,
            "",
            "## 口調ルール",
            f"- 一人称: {style.get('first_person') or '使わない（主語なし or 対象を主語にする）'}",
            f"- トーン: {style.get('tone', '')}",
            f"- 使ってよい語尾: {use_endings}",
            f"- 避ける語尾: {avoid_endings}",
            f"- 使ってよい絵文字: {preferred_emoji}",
            f"- 避ける絵文字: {avoid_emoji}",
            tone_rules,
            "",
            "## 制約",
            f"- {self.max_char_count}文字以内",
            f"- 最低{self.min_char_count}文字",
            "- 絵文字は0〜2個まで",
            "- 「！」は1投稿に最大1個",
            "- 漢字率30%以下",
            "- NGワード禁止（薬機法違反表現: 「治る」「消える」「効果がある」等）",
            "- 1行目（フック）で読者のスクロールを止めること",
            "",
            "## トピックタグ（必須）",
            f"- 本文中に {_CATEGORY_HASHTAGS.get(research_item.get('category', ''), '#スキンケア')} を1つだけ自然に組み込むこと",
            "- 末尾にポツンと置くのはNG。文中や文末の流れの中に溶け込ませる",
            "- 例: 「〜が #スキンケア の基本」「#美容成分 って聞くと難しそうだけど」",
            "",
            f"## ペルソナ: {persona_cfg.get('display_name', '')}",
            f"- タイプ: {persona_cfg.get('account_type', '匿名物知り系')}",
            f"- 権威: {persona_cfg.get('authority_style', '行動量ベース')}",
            "",
        ]

        # Inject TOP5/BOTTOM3 performance context
        perf_context = self._build_performance_context()
        if perf_context:
            prompt_parts.extend(["", perf_context, ""])

        # Inject debate theme context if provided
        if debate:
            talking_pts = "\n".join(
                f"- {tp}" for tp in debate.get("talking_points", [])
            )
            ng_stmts = "\n".join(
                f"- {ns}" for ns in debate.get("ng_statements", [])
            )
            prompt_parts.extend([
                "## 論争テーマ（このテーマで投稿を作成）",
                f"テーマ: {debate.get('title', '')}",
                f"立場: {debate.get('stance', '')}",
                "使える論点:",
                talking_pts,
                "",
                "## 禁止表現（絶対に使わないこと）",
                ng_stmts,
                "",
            ])

        if is_affiliate:
            prompt_parts.extend([
                "## アフィリエイトコメント指示",
                "この投稿にはPR用のアフィリエイトコメントを付けてください。",
                "本文とは別に、以下のフォーマットで出力してください:",
                "",
                "---本文---",
                "(ここに投稿本文)",
                "---アフィリエイトコメント---",
                "PR",
                "(ここにPRコメント。必ず1行目に「PR」と記載すること。)",
                "(商品リンクに誘導する自然な一言。押し売り感を出さない。)",
                "",
                "【重要】ステマ規制対応のため、アフィリエイトコメントの冒頭には必ず「PR」と明記してください。",
                "",
            ])
        else:
            prompt_parts.extend([
                "投稿本文のみを出力してください。余計な説明は不要です。",
                "",
            ])

        return "\n".join(prompt_parts)

    def _extract_pattern_template(self, pattern: str) -> str:
        """Extract the template section for the given pattern from posting_rules.md.

        Searches for ``#### パターンN: <pattern_name>`` and returns the text
        up to the next ``---`` separator.

        Args:
            pattern: The pattern name (e.g. ``"短文完結型"``).

        Returns:
            The extracted template text, or a generic fallback if not found.
        """
        # Find the pattern section in posting_rules.md
        marker = f": {pattern}"
        idx = self.posting_rules.find(marker)
        if idx == -1:
            return f"パターン: {pattern}\n（構成テンプレートに従って作成してください）"

        # Find the section end (next "---" separator)
        section_start = self.posting_rules.rfind("####", 0, idx)
        if section_start == -1:
            section_start = idx

        section_end = self.posting_rules.find("\n---\n", idx)
        if section_end == -1:
            section_end = min(section_start + 1000, len(self.posting_rules))

        return self.posting_rules[section_start:section_end].strip()

    def _build_performance_context(self) -> str:
        """Build a prompt section from audience.json TOP5/BOTTOM3 data."""
        data = self._audience_data
        if not data:
            return ""

        parts: list[str] = []
        top5 = data.get("top5", [])
        bottom3 = data.get("bottom3", [])
        feedback = data.get("feedback", {})

        if top5:
            parts.append("## 過去に伸びた投稿TOP5（構造を参考にせよ）\n")
            for i, p in enumerate(top5, 1):
                parts.append(
                    f"{i}. [engagement: {p.get('engagement_rate') or 0:.1f}% | "
                    f"category: {p.get('category', '')} | "
                    f"pattern: {p.get('pattern', '')}]\n"
                    f"   「{p.get('content_preview', '')}」"
                )

        if bottom3:
            parts.append("\n## 過去に伸びなかった投稿BOTTOM3（この構造は避けよ）\n")
            for i, p in enumerate(bottom3, 1):
                parts.append(
                    f"{i}. [engagement: {p.get('engagement_rate') or 0:.1f}% | "
                    f"category: {p.get('category', '')} | "
                    f"pattern: {p.get('pattern', '')}]"
                )

        insights = feedback.get("insights", [])
        if insights:
            parts.append("\n## 最近の傾向")
            for insight in insights[:3]:
                parts.append(f"- {insight}")

        return "\n".join(parts)

    def _build_prompt_with_hook(
        self,
        research_item: dict[str, Any],
        pattern: str,
        hook: str,
        *,
        is_affiliate: bool = False,
        debate: dict[str, Any] | None = None,
    ) -> str:
        """Build a prompt that incorporates a pre-selected hook as the first line."""
        base_prompt = self._build_prompt(
            research_item, pattern,
            is_affiliate=is_affiliate, debate=debate,
        )
        hook_instruction = (
            f"\n## 1行目（フック）指定\n"
            f"以下の文を投稿の1行目としてそのまま使用してください:\n"
            f"「{hook}」\n"
            f"この1行目に続く形で本文を作成してください。\n"
        )
        return base_prompt + hook_instruction

    def _build_qa_answer_prompt(
        self, research_item: dict[str, Any], question_text: str
    ) -> str:
        """Build prompt for answering a follower's question."""
        style = self.tone_config.get("style", {})
        tone_rules = "\n".join(f"- {r}" for r in style.get("rules", []))
        persona_cfg = self.tone_config.get("persona", {})

        return "\n".join([
            "## フォロワーからの質問",
            f"質問: {question_text}",
            "",
            "## 参考情報",
            f"- トピック: {research_item.get('topic', '')}",
            f"- 要約: {research_item.get('summary', '')}",
            "",
            "## 投稿パターン: フォロワー質問回答型",
            "構成:",
            "1行目: 質問への共感または引用（「〇〇って質問もらったんだけど」等）",
            "本文: 質問への具体的な回答（成分知識・根拠を含む）（3-5行）",
            "最終行: シグネチャー語尾またはフォローアップの問いかけ",
            "",
            "## 口調ルール",
            f"- 一人称: {style.get('first_person') or '使わない（主語なし or 対象を主語にする）'}",
            f"- トーン: {style.get('tone', '')}",
            tone_rules,
            "",
            "## 制約",
            f"- {self.max_char_count}文字以内",
            f"- 最低{self.min_char_count}文字",
            "- NGワード禁止",
            "- 投稿本文のみを出力（説明不要）",
            "",
            f"## ペルソナ: {persona_cfg.get('display_name', '')}",
            "",
        ])
