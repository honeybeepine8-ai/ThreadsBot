"""WriterDraftMixin — draft queue management and auto-approve routing."""

from __future__ import annotations

import datetime
from typing import Any


class WriterDraftMixin:
    """Mixin providing draft-queue enqueue and auto-approve logic."""

    def _enqueue_drafts(
        self,
        post_items: list[dict[str, Any]],
        now: datetime.datetime,
    ) -> None:
        """Add generated posts to ``draft_queue.json`` with review routing.

        Loads state files once, processes all items in-memory, and persists
        once at the end to avoid N+1 file reads.

        Auto-approve rule:
        - quality_score >= auto_approve_threshold (default 8.5)
          and no review/fact-check flags
          -> status ``"approved"``, also added to ``post_queue.json``
        - Otherwise -> status ``"pending_review"`` (awaits human review)
        """
        if not post_items:
            return

        draft_data = self.state.load_json("draft_queue.json")
        queue_data = self.state.load_json("post_queue.json")
        stats = draft_data.setdefault("stats", {})
        queue_changed = False

        for post_item in post_items:
            score = post_item.get("quality_score", 0.0)
            fact_check = post_item.get("fact_check_required", False)
            pharma_borderline = post_item.get("pharma_law_check") == "borderline"
            ng_review = post_item.get("ng_review_required", False)
            review_required = fact_check or pharma_borderline or ng_review

            auto_approved = (
                score >= self.auto_approve_threshold
                and not review_required
            )

            draft_id = post_item["id"].replace("q_", "d_", 1)
            expires_at = now + datetime.timedelta(hours=self.draft_expire_hours)

            draft_item: dict[str, Any] = {
                **post_item,
                "id": draft_id,
                "post_queue_id": post_item["id"],
                "flags": {
                    "auto_approved": auto_approved,
                    "review_required": review_required,
                    "fact_check_required": fact_check,
                    "pharma_law_check": post_item.get("pharma_law_check", "safe"),
                },
                "expires_at": expires_at.isoformat(),
                "status": "approved" if auto_approved else "pending_review",
                "review": None,
            }

            draft_data.setdefault("drafts", []).append(draft_item)
            stats["total_generated"] = stats.get("total_generated", 0) + 1

            if auto_approved:
                stats["auto_approved"] = stats.get("auto_approved", 0) + 1
                queue_data.setdefault("queue", []).append(post_item)
                queue_changed = True
                self.logger.info(
                    "Draft %s auto-approved (score=%.1f) -> post_queue.",
                    draft_id, score,
                )
            else:
                self.logger.info(
                    "Draft %s pending review (score=%.1f, review_required=%s).",
                    draft_id, score, review_required,
                )

        # Persist once
        draft_data["last_updated"] = now.isoformat()
        self.state.save_json("draft_queue.json", draft_data)
        if queue_changed:
            queue_data["last_updated"] = now.isoformat()
            self.state.save_json("post_queue.json", queue_data)
