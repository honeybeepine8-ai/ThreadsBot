"""WriterContentMixin — debate, UGC, hook A/B test, and QA logic."""

from __future__ import annotations

import datetime
import json as _json
import random
import re
from typing import Any

from core.constants import JST

_DEBATE_PATTERNS: set[str] = {"反常識型", "コメント誘導型", "二択・比較型"}


class WriterContentMixin:
    """Mixin providing debate injection, UGC, hook A/B, and QA methods."""

    # ==================================================================
    # Debate theme injection
    # ==================================================================

    def _maybe_inject_debate_theme(self, pattern: str) -> dict[str, Any] | None:
        """Optionally inject a debate theme for patterns that benefit from it.

        Only triggers for debate-compatible patterns (反常識型, コメント誘導型,
        二択・比較型) at a configurable probability.

        Returns:
            A debate dict from debate_whitelist.yaml, or None.
        """
        if pattern not in _DEBATE_PATTERNS:
            return None

        try:
            config = self._load_yaml("knowledge/debate_whitelist.yaml")
        except Exception:
            return None

        integration = config.get("integration", {})
        injection_rate = integration.get("debate_injection_rate", 0.4)

        if random.random() >= injection_rate:
            return None

        min_days = integration.get("min_days_between_same_debate", 7)
        recent_ids = self._get_recent_debate_ids(days=min_days)

        debates: list[dict[str, Any]] = config.get("debates", [])
        candidates = [
            d for d in debates
            if d["id"] not in recent_ids
            and pattern in d.get("recommended_patterns", [])
        ]

        if not candidates:
            return None

        chosen = random.choice(candidates)
        self.logger.info(
            "Injecting debate theme: %s (%s)",
            chosen.get("title", "?"),
            chosen.get("id", "?"),
        )
        return chosen

    def _get_recent_debate_ids(self, days: int = 7) -> set[str]:
        """Return debate IDs used in the last *days* days."""
        now = datetime.datetime.now(JST)
        cutoff = now - datetime.timedelta(days=days)

        recent_ids: set[str] = set()

        # Check post_history
        history_data = self.state.load_json("post_history.json")
        for post in history_data.get("posts", []):
            debate_id = post.get("debate_id")
            if not debate_id:
                continue
            try:
                posted_at = datetime.datetime.fromisoformat(post["posted_at"])
                if posted_at >= cutoff:
                    recent_ids.add(debate_id)
            except (ValueError, TypeError, KeyError):
                pass

        # Also check pending queue
        queue_data = self.state.load_json("post_queue.json")
        for item in queue_data.get("queue", []):
            if item.get("status") == "pending" and item.get("debate_id"):
                recent_ids.add(item["debate_id"])

        return recent_ids

    # ==================================================================
    # UGC template generation
    # ==================================================================

    def _should_generate_ugc(self) -> bool:
        """Check if today is a UGC template posting day and one hasn't been created yet."""
        from agents.writer_constants import _WEEKDAY_NAMES

        try:
            self._resolve_path("config/ugc_templates.yaml")
        except FileNotFoundError:
            return False

        now = datetime.datetime.now(JST)
        today_weekday = _WEEKDAY_NAMES[now.weekday()]

        ugc_config = self._load_yaml("config/ugc_templates.yaml")
        templates: list[dict[str, Any]] = ugc_config.get("templates", [])

        for tmpl in templates:
            schedule_day = tmpl.get("schedule_day", "")
            if schedule_day == today_weekday:
                # Check if we already generated a UGC post today
                queue_data = self.state.load_json("post_queue.json")
                today_str = now.strftime("%Y-%m-%d")
                for item in queue_data.get("queue", []):
                    if (item.get("pattern") == "UGCテンプレ型"
                            and item.get("created_at", "").startswith(today_str)):
                        return False
                return True
        return False

    def _generate_ugc_post(self, now: datetime.datetime) -> dict[str, Any] | None:
        """Generate a UGC template post for today's scheduled template."""
        from agents.writer_constants import _WEEKDAY_NAMES

        try:
            self._resolve_path("config/ugc_templates.yaml")
        except FileNotFoundError:
            return None

        today_weekday = _WEEKDAY_NAMES[now.weekday()]

        ugc_config = self._load_yaml("config/ugc_templates.yaml")
        templates: list[dict[str, Any]] = ugc_config.get("templates", [])

        # Find the template for today
        target_tmpl: dict[str, Any] | None = None
        for tmpl in templates:
            if tmpl.get("schedule_day") == today_weekday:
                target_tmpl = tmpl
                break

        if not target_tmpl:
            return None

        template_text = target_tmpl.get("template", "").strip()
        hashtag = target_tmpl.get("hashtag", "#成分チャレンジ")
        tmpl_name = target_tmpl.get("name", "UGCテンプレ")

        # Use Claude to fill in the template as our anonymous persona
        prompt = (
            f"## UGCテンプレート\n{template_text}\n\n"
            f"## 指示\n"
            f"上記のテンプレートの「___」部分を匿名の成分オタクとして埋めてください。\n"
            f"- 実際に使っている（という設定の）具体的な商品名や方法を入れる\n"
            f"- フォロワーが自分バージョンを引用リポストしたくなる内容にする\n"
            f"- 投稿本文のみを出力（説明不要）\n"
            f"- 末尾に必ず「{hashtag}」を含める\n"
            f"- 500文字以内\n"
        )

        try:
            raw_content = self.claude_client.generate_post(
                prompt=prompt,
                system_prompt=self.system_prompt,
            )
            content = self._clean_content(raw_content).strip()

            # Ensure hashtag is present
            if hashtag not in content:
                content += f"\n\n{hashtag}"

            if len(content) < self.min_char_count or len(content) > self.max_char_count:
                self.logger.warning("UGC post length out of range: %d", len(content))
                return None

            # Select evening slot for UGC (engagement-optimized)
            time_slot = self._select_time_slot()

            date_str = now.strftime("%Y%m%d")
            queue_data = self.state.load_json("post_queue.json")
            seq = len(queue_data.get("queue", [])) + 1

            return {
                "id": f"q_{date_str}_{seq:03d}",
                "research_id": f"ugc_{tmpl_name}",
                "content": content,
                "hashtag": hashtag,
                "pattern": "UGCテンプレ型",
                "quality_score": 8.0,
                "similarity_score": 0.0,
                "category": "skincare_routine",
                "scheduled_at": time_slot,
                "created_at": now.isoformat(),
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": None,
                "profile_cta_included": False,
                "cta_pr_label": False,
                "debate_id": None,
                "debate_title": None,
            }

        except Exception as exc:
            self.logger.error("UGC post generation failed: %s", exc)
            return None

    # ==================================================================
    # Hook A/B test
    # ==================================================================

    def _generate_hook_candidates(
        self,
        research_item: dict[str, Any],
        pattern: str,
        debate: dict[str, Any] | None = None,
    ) -> list[str]:
        """Generate multiple hook (first-line) candidates for A/B testing."""
        # Load reference hooks from stock
        hook_stock = self._load_hook_stock()
        reference_hooks: list[str] = []
        cat = research_item.get("category", "")
        for h in hook_stock:
            if h.get("category") == cat:
                reference_hooks.append(h["text"])
            if len(reference_hooks) >= 5:
                break

        reference_section = ""
        if reference_hooks:
            examples = "\n".join(f"- {h}" for h in reference_hooks)
            reference_section = (
                f"\n\n## 参考: 過去にバズったフック文の例\n{examples}\n"
                "上記を参考にしつつ、同じ文は使わず新しいフック文を生成してください。"
            )

        debate_context = ""
        if debate:
            debate_context = f"\n論争テーマ: {debate.get('title', '')}"

        prompt = (
            f"## タスク\n"
            f"以下のネタについて、Threads投稿の1行目（フック文）の候補を"
            f"{self.hook_candidates_count}個生成してください。\n\n"
            f"## ネタ\n"
            f"トピック: {research_item.get('topic', '')}\n"
            f"要約: {research_item.get('summary', '')}\n"
            f"パターン: {pattern}\n"
            f"{debate_context}\n\n"
            f"## フック文の条件\n"
            f"- 40文字以内\n"
            f"- 読者のスクロールを止める力があること\n"
            f"- 疑問形、意外性、共感のいずれかの要素を含む\n"
            f"- 各フック文は改行で区切って出力\n"
            f"- フック文のみを出力（番号や説明は不要）"
            f"{reference_section}"
        )

        try:
            raw = self.claude_client.generate_post(prompt=prompt)
            lines = [
                line.strip()
                for line in raw.strip().split("\n")
                if line.strip() and len(line.strip()) >= 5
            ]
            cleaned: list[str] = []
            for line in lines:
                line = re.sub(r"^[\d①②③④⑤]+[.\)）\s]+", "", line).strip()
                if line:
                    cleaned.append(line)
            return cleaned[: self.hook_candidates_count]
        except Exception as exc:
            self.logger.warning("Hook candidate generation failed: %s", exc)
            return []

    def _score_hooks(
        self,
        hooks: list[str],
        research_item: dict[str, Any],
    ) -> str | None:
        """Score hook candidates using Claude and return the best one."""
        if not hooks:
            return None
        if len(hooks) == 1:
            return hooks[0]

        hooks_text = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(hooks))

        prompt = (
            f"## タスク\n"
            f"以下のフック文（Threads投稿の1行目）候補をスコアリングしてください。\n\n"
            f"## ネタのコンテキスト\n"
            f"トピック: {research_item.get('topic', '')}\n\n"
            f"## フック文候補\n{hooks_text}\n\n"
            f"## 評価基準\n"
            f"1. スクロール停止力（思わず止まるか）\n"
            f"2. 好奇心の喚起（続きが読みたくなるか）\n"
            f"3. 自然さ（人間が書いたように見えるか）\n\n"
            f"## 出力形式\n"
            f"JSON形式で各候補のスコア(1-10)と最良の番号を返してください:\n"
            f'{{"scores": [<score1>, <score2>, ...], "best": <1-indexed number>}}\n'
            f"JSONのみを返してください。"
        )

        try:
            raw = self.claude_client.generate_post(prompt=prompt)
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                return hooks[0]

            data = _json.loads(text[start : end + 1])
            best_idx = data.get("best", 1) - 1

            if 0 <= best_idx < len(hooks):
                self.logger.info(
                    "Hook A/B test: selected #%d (scores=%s)",
                    best_idx + 1,
                    data.get("scores", []),
                )
                return hooks[best_idx]

            return hooks[0]
        except Exception as exc:
            self.logger.warning("Hook scoring failed: %s — using first candidate", exc)
            return hooks[0]

    def _load_hook_stock(self) -> list[dict[str, Any]]:
        """Load hook stock from knowledge/hook_stock.json."""
        hook_path = self.ctx.knowledge_dir / "hook_stock.json"
        try:
            with open(hook_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            hooks = data.get("hooks", [])
            hooks.sort(key=lambda h: h.get("engagement_rate", 0), reverse=True)
            return hooks
        except (FileNotFoundError, _json.JSONDecodeError):
            return []

    # ==================================================================
    # QA solicitation / answer cycle
    # ==================================================================

    def _should_generate_qa_solicitation(self) -> bool:
        """Check if today is a QA solicitation posting day."""
        from agents.writer_constants import _WEEKDAY_NAMES

        now = datetime.datetime.now(JST)
        weekday = _WEEKDAY_NAMES[now.weekday()]

        if weekday not in self.qa_schedule_days:
            return False

        # Check if already generated today
        today_str = now.strftime("%Y-%m-%d")
        queue_data = self.state.load_json("post_queue.json")
        for item in queue_data.get("queue", []):
            if (
                item.get("pattern") == "質問募集型"
                and item.get("created_at", "").startswith(today_str)
            ):
                return False

        history_data = self.state.load_json("post_history.json")
        for post in history_data.get("posts", []):
            if (
                post.get("pattern") == "質問募集型"
                and post.get("posted_at", "").startswith(today_str)
            ):
                return False

        return True

    def _generate_qa_solicitation_post(
        self, now: datetime.datetime
    ) -> dict[str, Any] | None:
        """Generate a '質問募集型' post that asks followers for questions."""
        prompt = (
            "## 投稿パターン: 質問募集型\n"
            "フォロワーからスキンケアに関する質問を募集する投稿を作成してください。\n\n"
            "## 構成\n"
            "1行目: フック（読者の興味を引く）\n"
            "本文2〜3行: テーマの提示や最近気になること\n"
            "最終行: 「コメントで教えて」系の質問募集CTA\n\n"
            "## 制約\n"
            f"- {self.max_char_count}文字以内\n"
            f"- 最低{self.min_char_count}文字\n"
            "- 絵文字は0〜2個まで\n"
            "- 投稿本文のみを出力（説明不要）\n"
            "- 「〇〇枚読んだ」「〇〇年読んでる」等、自分の行動量・経験量を数値で語る表現は使わない\n"
        )

        try:
            raw_content = self.claude_client.generate_post(
                prompt=prompt, system_prompt=self.system_prompt,
            )
            content = self._clean_content(raw_content).strip()

            if len(content) < self.min_char_count or len(content) > self.max_char_count:
                return None

            ng = self.quality_gate.check_ng_words(content)
            if ng:
                return None

            time_slot = self._select_time_slot()
            date_str = now.strftime("%Y%m%d")
            queue_data = self.state.load_json("post_queue.json")
            seq = len(queue_data.get("queue", [])) + 1

            return {
                "id": f"q_{date_str}_{seq:03d}",
                "research_id": "qa_solicitation",
                "content": content,
                "hashtag": "#スキンケア",
                "pattern": "質問募集型",
                "quality_score": 8.0,
                "similarity_score": 0.0,
                "category": "skincare_knowledge",
                "scheduled_at": time_slot,
                "created_at": now.isoformat(),
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": None,
                "profile_cta_included": False,
                "cta_pr_label": False,
                "debate_id": None,
                "debate_title": None,
            }
        except Exception as exc:
            self.logger.error("QA solicitation post generation failed: %s", exc)
            return None

    def _generate_qa_answer_post(
        self,
        research_item: dict[str, Any],
        now: datetime.datetime,
        time_slot: str,
    ) -> dict[str, Any] | None:
        """Generate a post answering a follower's question from the research pool."""
        from agents.writer_constants import _CATEGORY_HASHTAGS, _PROFILE_CTA_TEMPLATES

        history_data = self.state.load_json("post_history.json")
        recent_posts = history_data.get("posts", [])[-self.similarity_compare_count :]

        question_text = research_item.get(
            "question_text", research_item.get("topic", "")
        )

        for attempt in range(1, self.max_generation_attempts + 1):
            prompt = self._build_qa_answer_prompt(research_item, question_text)
            raw_content = self.claude_client.generate_post(
                prompt=prompt, system_prompt=self.system_prompt,
            )
            content = self._clean_content(raw_content)

            if len(content) < self.min_char_count or len(content) > self.max_char_count:
                continue

            gate_result = self.quality_gate.validate(
                content=content,
                pattern="フォロワー質問回答型",
                post_history=recent_posts,
            )
            if not gate_result["passed"]:
                continue

            quality_eval = self._evaluate_quality_score(content)
            avg_score = quality_eval.get("average", 0.0)
            if avg_score < self.min_quality_score:
                continue

            # Answering questions is a natural CTA opportunity
            profile_cta_included = False
            if random.random() < self.profile_cta_rate:
                cta = random.choice(_PROFILE_CTA_TEMPLATES)
                if len(content) + len(cta) <= self.max_char_count:
                    content += cta
                    profile_cta_included = True

            date_str = now.strftime("%Y%m%d")
            queue_data = self.state.load_json("post_queue.json")
            seq = len(queue_data.get("queue", [])) + 1
            category = research_item.get("category", "skincare_knowledge")
            hashtag = _CATEGORY_HASHTAGS.get(category, "#スキンケア")

            return {
                "id": f"q_{date_str}_{seq:03d}",
                "research_id": research_item["id"],
                "content": content,
                "hashtag": hashtag,
                "pattern": "フォロワー質問回答型",
                "quality_score": round(avg_score, 1),
                "similarity_score": round(
                    gate_result.get("similarity_score", 0.0), 2
                ),
                "category": category,
                "scheduled_at": time_slot,
                "created_at": now.isoformat(),
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": None,
                "profile_cta_included": profile_cta_included,
                "cta_pr_label": profile_cta_included,
                "debate_id": None,
                "debate_title": None,
                "source_question": question_text,
            }

        return None
