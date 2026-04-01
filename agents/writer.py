"""Writer agent — generates Threads posts, validates quality, and enqueues them."""

from __future__ import annotations

import datetime
import random
from typing import Any

from agents.base_agent import BaseAgent
from agents.writer_constants import (
    _CATEGORY_HASHTAGS,
    _PROFILE_CTA_TEMPLATES,
    load_audience_data,
    load_text,
    load_yaml,
)
from agents.writer_content import WriterContentMixin
from agents.writer_draft import WriterDraftMixin
from agents.writer_pattern import WriterPatternMixin
from agents.writer_prompt import WriterPromptMixin
from agents.writer_quality import WriterQualityMixin
from agents.writer_thread import WriterThreadMixin
from core.boost import (
    get_effective_schedule,
    get_effective_writer_config,
)
from core.constants import JST
from core.fact_checker import FactChecker
from core.logger import get_logger
from core.quality_gate import QualityGate
from services.claude_client import ClaudeClient

logger = get_logger("writer")


class WriterAgent(
    WriterPatternMixin,
    WriterPromptMixin,
    WriterQualityMixin,
    WriterThreadMixin,
    WriterContentMixin,
    WriterDraftMixin,
    BaseAgent,
):
    """Generate Threads posts from the research pool and enqueue them.

    This agent is the main content engine.  It reads research items, produces
    posts via the Claude API, runs them through quality and safety gates, and
    writes them into ``draft_queue.json`` for human review.

    Posts with quality score >= 8.5 are auto-approved and moved directly to
    ``post_queue.json``.  Lower-scoring posts wait for human review via
    ``scripts/review.py``.
    """

    def __init__(self, ctx=None) -> None:
        super().__init__("writer", ctx=ctx)

        # External services / gates
        self.claude_client = ClaudeClient()
        self.quality_gate = QualityGate()
        self.fact_checker = FactChecker(claude_client=self.claude_client)

        # Load configuration files (account-aware)
        self.tone_config: dict[str, Any] = self._load_yaml("config/tone.yaml")
        self.schedule_config: dict[str, Any] = self._load_yaml("config/schedule.yaml")
        self.settings: dict[str, Any] = self._load_yaml("config/settings.yaml")

        # Apply boost mode overrides if active
        self.schedule_config = get_effective_schedule(self.schedule_config)

        # Writer-specific settings (with sensible defaults, boost-aware)
        writer_cfg: dict[str, Any] = get_effective_writer_config(
            self.settings.get("writer", {})
        )
        self.queue_target_size: int = writer_cfg.get("queue_target_size", 10)
        self.max_generation_attempts: int = writer_cfg.get("max_generation_attempts", 5)
        self.recent_pattern_block: int = writer_cfg.get("recent_pattern_block", 3)
        self.min_char_count: int = writer_cfg.get("min_char_count", 40)
        self.max_char_count: int = writer_cfg.get("max_char_count", 500)
        self.pr_ratio: float = writer_cfg.get("pr_ratio", 0.0)
        self.profile_cta_rate: float = writer_cfg.get("profile_cta_rate", 0.0)
        self.thread_ratio: float = writer_cfg.get("thread_ratio", 0.0)
        self.thread_min_posts: int = writer_cfg.get("thread_min_posts", 2)
        self.thread_max_posts: int = writer_cfg.get("thread_max_posts", 3)

        # Hook A/B test settings
        hook_cfg: dict[str, Any] = writer_cfg.get("hook_ab_test", {})
        self.hook_ab_enabled: bool = hook_cfg.get("enabled", True)
        self.hook_candidates_count: int = hook_cfg.get("candidates_count", 3)

        # QA solicitation settings
        qa_cfg: dict[str, Any] = writer_cfg.get("qa_solicitation", {})
        self.qa_schedule_days: list[str] = qa_cfg.get(
            "schedule_days", ["tuesday", "friday"]
        )

        safety_cfg: dict[str, Any] = self.settings.get("safety", {})
        self.min_quality_score: float = safety_cfg.get("min_quality_score", 7.0)
        self.similarity_compare_count: int = safety_cfg.get("similarity_compare_count", 50)

        # Draft review settings
        draft_cfg: dict[str, Any] = writer_cfg.get("draft_review", {})
        self.auto_approve_threshold: float = draft_cfg.get(
            "auto_approve_threshold", 8.5
        )
        self.auto_approve_timeout_threshold: float = draft_cfg.get(
            "auto_approve_timeout_threshold", 8.0
        )
        self.draft_expire_hours: int = draft_cfg.get("expire_hours", 12)

        # Load knowledge / prompt templates (account-aware)
        self.posting_rules: str = self._load_text("knowledge/posting_rules.md")
        self.system_prompt: str = self._load_text("prompts/writer.md")

        # Load audience feedback (TOP5/BOTTOM3 for prompt injection)
        self._audience_data: dict[str, Any] = self._load_audience_data()

        self.logger.info(
            "WriterAgent initialised (queue_target=%d, max_attempts=%d, auto_approve>=%.1f)",
            self.queue_target_size,
            self.max_generation_attempts,
            self.auto_approve_threshold,
        )

    # ==================================================================
    # Helpers
    # ==================================================================

    def _load_audience_data(self) -> dict[str, Any]:
        """Load audience.json if it exists (account-aware)."""
        import json as _json
        path = self.ctx.analytics_dir / "audience.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return _json.load(f)
            except Exception:
                pass
        return {}

    # ==================================================================
    # Public entry point
    # ==================================================================

    def execute(self) -> None:
        """Batch-generate posts to fill the queue up to *queue_target_size*."""
        now = datetime.datetime.now(JST)

        # --- 1. Check current queue depth (post_queue + draft_queue) ---
        queue_data: dict[str, Any] = self.state.load_json("post_queue.json")
        queue: list[dict[str, Any]] = queue_data.get("queue", [])
        post_pending = sum(1 for item in queue if item.get("status") == "pending")

        draft_data: dict[str, Any] = self.state.load_json("draft_queue.json")
        drafts: list[dict[str, Any]] = draft_data.get("drafts", [])
        draft_pending = sum(
            1 for d in drafts if d.get("status") == "pending_review"
        )

        total_pending = post_pending + draft_pending
        if total_pending >= self.queue_target_size:
            self.logger.info(
                "Queues already have %d pending (post=%d, draft=%d, target=%d). Nothing to do.",
                total_pending, post_pending, draft_pending, self.queue_target_size,
            )
            return

        needed = self.queue_target_size - total_pending
        self.logger.info(
            "Queues have %d pending (post=%d, draft=%d); need %d more to reach target %d.",
            total_pending, post_pending, draft_pending, needed, self.queue_target_size,
        )

        # --- 1b. Collect special posts (UGC, QA) ---
        special_posts: list[dict[str, Any]] = []
        if self._should_generate_ugc():
            ugc_post = self._generate_ugc_post(now)
            if ugc_post:
                special_posts.append(ugc_post)

        if self._should_generate_qa_solicitation():
            qa_post = self._generate_qa_solicitation_post(now)
            if qa_post:
                special_posts.append(qa_post)

        if special_posts:
            self._enqueue_drafts(special_posts, now)

        # --- 2. Fetch unused research items ---
        pool_data: dict[str, Any] = self.state.load_json("research_pool.json")
        pool_items: list[dict[str, Any]] = pool_data.get("items", [])
        unused_items = [
            item for item in pool_items
            if not item.get("used", False)
        ]
        unused_items.sort(key=lambda x: x.get("priority", 0), reverse=True)

        if not unused_items:
            self.logger.warning("No unused research items in pool. Cannot generate posts.")
            return

        # --- 3. Generate posts (incremental save: 1件ごとに即保存) ---
        generated_count = 0

        for research_item in unused_items:
            if generated_count >= needed:
                break

            self.logger.info(
                "Generating post for research item '%s': %s",
                research_item.get("id", "?"), research_item.get("topic", "?"),
            )

            post = self._generate_single_post(research_item)
            if post is not None:
                self._enqueue_drafts([post], now)
                generated_count += 1
                self.logger.info(
                    "Post generated and saved: id=%s, score=%.1f (%d/%d)",
                    post["id"], post["quality_score"], generated_count, needed,
                )

                for item in pool_data.get("items", []):
                    if item.get("id") == research_item["id"]:
                        item["used"] = True
                        break
                pool_data["last_updated"] = now.isoformat()
                self.state.save_json("research_pool.json", pool_data)
            else:
                self.logger.warning(
                    "Failed to generate acceptable post for research '%s'.",
                    research_item.get("id", "?"),
                )

        if generated_count:
            self.logger.info("Total: %d posts added to the draft queue.", generated_count)

    # ==================================================================
    # Single-post generation pipeline
    # ==================================================================

    def _generate_single_post(self, research_item: dict[str, Any]) -> dict[str, Any] | None:
        """Attempt to generate one quality-checked post from a research item."""
        now = datetime.datetime.now(JST)
        pattern = self._select_pattern()
        time_slot = self._select_time_slot()
        is_pr_slot = self._is_pr_slot(time_slot)

        debate_theme = self._maybe_inject_debate_theme(pattern)

        # Thread format: delegate to specialized generator
        if pattern == "ツリー展開型" and self.thread_ratio > 0:
            return self._generate_thread_post(
                research_item, now, time_slot, is_pr_slot, debate_theme,
            )

        # QA answer format: delegate to specialized generator
        if pattern == "フォロワー質問回答型":
            return self._generate_qa_answer_post(research_item, now, time_slot)

        # Load recent post history for quality gate and topic check
        history_data: dict[str, Any] = self.state.load_json("post_history.json")
        recent_posts: list[dict[str, Any]] = history_data.get("posts", [])
        recent_posts = recent_posts[-self.similarity_compare_count:]

        # Topic repetition check: skip if same category+keyword appeared in last 3 posts
        if self._is_topic_repeated(research_item, recent_posts):
            self.logger.debug(
                "Topic repeat detected for '%s' — skipping.",
                research_item.get("id", "?"),
            )
            return None

        for attempt in range(1, self.max_generation_attempts + 1):
            self.logger.debug(
                "Generation attempt %d/%d for research '%s' (pattern=%s)",
                attempt, self.max_generation_attempts,
                research_item.get("id", "?"), pattern,
            )

            # 1. Build prompt and generate content (with optional hook A/B test)
            selected_hook: str | None = None
            if self.hook_ab_enabled and attempt == 1:
                hooks = self._generate_hook_candidates(
                    research_item, pattern, debate_theme,
                )
                if hooks:
                    selected_hook = self._score_hooks(hooks, research_item)

            if selected_hook:
                prompt = self._build_prompt_with_hook(
                    research_item, pattern, selected_hook,
                    is_affiliate=is_pr_slot, debate=debate_theme,
                )
            else:
                prompt = self._build_prompt(
                    research_item, pattern,
                    is_affiliate=is_pr_slot, debate=debate_theme,
                )

            raw_content = self.claude_client.generate_post(
                prompt=prompt, system_prompt=self.system_prompt,
            )
            content = self._clean_content(raw_content)

            # 2. Basic length validation
            if len(content) < self.min_char_count or len(content) > self.max_char_count:
                self.logger.debug(
                    "Content length %d out of range [%d, %d]. Retrying.",
                    len(content), self.min_char_count, self.max_char_count,
                )
                continue

            # 3. Quality gate (NG words, similarity, pattern rotation)
            gate_result = self.quality_gate.validate(
                content=content, pattern=pattern, post_history=recent_posts,
            )
            if not gate_result["passed"]:
                self.logger.debug(
                    "Quality gate rejected: %s. Retrying.", gate_result["reason"]
                )
                if "pattern_repeated" in gate_result.get("reason", ""):
                    pattern = self._select_pattern()
                continue

            # 3b. Debate NG statements check
            if debate_theme:
                ng_stmts = debate_theme.get("ng_statements", [])
                if ng_stmts:
                    debate_check = self.quality_gate.check_debate_ng(content, ng_stmts)
                    if not debate_check["passed"]:
                        self.logger.debug(
                            "Debate NG check failed: %s. Retrying.",
                            debate_check["reason"],
                        )
                        continue

            # 4. AI quality evaluation
            quality_eval = self._evaluate_quality_score(content)
            avg_score: float = quality_eval.get("average", 0.0)

            if avg_score < self.min_quality_score:
                self.logger.debug(
                    "Quality score %.1f < %.1f (min). Retrying.",
                    avg_score, self.min_quality_score,
                )
                continue

            # 5. Fact check (pharma law + numerical claims)
            fact_result = self.fact_checker.run_all_checks(content)

            if fact_result["blocked"]:
                self.logger.warning(
                    "Pharma law violation — blocked: %s. Retrying.",
                    fact_result["block_reason"],
                )
                continue

            # 6. All checks passed — build queue item
            affiliate_comment: str | None = None
            follow_up_comment: str | None = None
            if is_pr_slot:
                affiliate_comment = self._extract_affiliate_comment(raw_content)
            elif pattern == "コメント誘導型":
                follow_up_comment = self._extract_follow_up_comment(raw_content)

            profile_cta_included = False
            if not is_pr_slot and random.random() < self.profile_cta_rate:
                cta = random.choice(_PROFILE_CTA_TEMPLATES)
                if len(content) + len(cta) <= self.max_char_count:
                    content += cta
                    profile_cta_included = True

            date_str = now.strftime("%Y%m%d")
            queue_data = self.state.load_json("post_queue.json")
            existing_queue = queue_data.get("queue", [])
            seq = len(existing_queue) + 1

            category = research_item.get("category", "skincare_knowledge")
            hashtag = _CATEGORY_HASHTAGS.get(category, "#スキンケア")

            post_item: dict[str, Any] = {
                "id": f"q_{date_str}_{seq:03d}",
                "research_id": research_item["id"],
                "content": content,
                "hashtag": hashtag,
                "pattern": pattern,
                "quality_score": round(avg_score, 1),
                "similarity_score": round(gate_result.get("similarity_score", 0.0), 2),
                "category": category,
                "scheduled_at": time_slot,
                "created_at": now.isoformat(),
                "status": "pending",
                "retry_count": 0,
                "affiliate_comment": affiliate_comment,
                "follow_up_comment": follow_up_comment,
                "profile_cta_included": profile_cta_included,
                "cta_pr_label": profile_cta_included or bool(affiliate_comment),
                "debate_id": debate_theme["id"] if debate_theme else None,
                "debate_title": debate_theme["title"] if debate_theme else None,
                "fact_check_required": fact_result["fact_check_required"],
                "fact_check_unknown_claims": fact_result["numerical"]["unknown_claims"],
                "pharma_law_check": fact_result["pharma_law"]["result"],
                "ng_review_required": gate_result.get("review_required", False),
            }
            return post_item

        # All attempts exhausted
        return None

    # ------------------------------------------------------------------
    # Topic repetition guard (F3)
    # ------------------------------------------------------------------

    _TOPIC_REPEAT_WINDOW = 3

    def _is_topic_repeated(
        self,
        research_item: dict[str, Any],
        recent_posts: list[dict[str, Any]],
    ) -> bool:
        """Return True if the same category+primary-keyword appeared in recent posts.

        Prevents the same topic (e.g. "セラミド") from being published
        in consecutive posts.

        Args:
            research_item: The candidate research item.
            recent_posts: Most recent post history records (already sliced).

        Returns:
            True if the topic is considered repeated and should be skipped.
        """
        item_category = research_item.get("category", "")
        item_keywords: list[str] = research_item.get("keywords", [])
        if not item_keywords:
            return False
        primary_kw = item_keywords[0].lower()

        for post in recent_posts[-self._TOPIC_REPEAT_WINDOW:]:
            if post.get("category", "") != item_category:
                continue
            post_content = post.get("content", "").lower()
            if primary_kw and primary_kw in post_content:
                return True
        return False
