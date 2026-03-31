"""Poster agent — publishes scheduled posts to Threads.

Supports both single posts and thread-format posts (multiple self-reply
posts chained together).
"""

from __future__ import annotations

import datetime
import time

from agents.base_agent import BaseAgent
from core.constants import JST
from core.notifier import Notifier, SEVERITY_HIGH
from services.threads_api import ThreadsAPIClient

# Delay between thread posts to avoid API rate limits
_THREAD_POST_DELAY_SECONDS = 5


class PosterAgent(BaseAgent):
    """Pick the next pending post from the queue and publish it via Threads API.

    On success the post is recorded in ``post_history.json``, the queue entry
    is marked ``"posted"``, and safety counters are updated.  If the queued
    item contains an ``affiliate_comment`` field the agent will publish a
    self-reply with that text immediately after the main post.
    """

    def __init__(self, ctx=None) -> None:
        super().__init__("poster", ctx=ctx)
        self.threads = ThreadsAPIClient()
        self.notifier = Notifier()

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def execute(self) -> None:
        """Publish one pending post whose ``scheduled_at`` is in the past."""
        now = datetime.datetime.now(JST)

        # 1. Load post queue and pick the first eligible item
        queue_data = self.state.load_json("post_queue.json")
        queue: list[dict] = queue_data.get("queue", [])

        target: dict | None = None
        for item in queue:
            if item.get("status") != "pending":
                continue
            try:
                scheduled_at = datetime.datetime.fromisoformat(item["scheduled_at"])
            except (KeyError, ValueError, TypeError):
                self.logger.warning(
                    "Skipping queue item %s: invalid scheduled_at.",
                    item.get("id", "?"),
                )
                continue
            if scheduled_at <= now:
                target = item
                break

        if target is None:
            self.logger.info("No pending posts ready to publish.")
            return

        # 2. Safety check — are we allowed to post right now?
        can, reason = self.safety.can_post()
        if not can:
            self.logger.warning("Safety guard blocked posting: %s", reason)
            return

        # 3. Publish the post via Threads API
        content: str = target["content"]
        self.logger.info("Publishing post %s …", target["id"])
        try:
            result = self.threads.create_text_post(content)
        except Exception as exc:
            self.notifier.send(
                "post_failed",
                f"Post {target['id']} failed: {exc}",
                SEVERITY_HIGH,
            )
            raise

        threads_media_id: str = result.get("id", "")
        posted_at = datetime.datetime.now(JST).isoformat()

        # 3b. Publish thread follow-up posts (if any)
        thread_posts: list[str] = target.get("thread_posts") or []
        thread_media_ids: list[str] = []
        if thread_posts:
            parent_id = threads_media_id
            for i, thread_content in enumerate(thread_posts):
                self.logger.info(
                    "Publishing thread post %d/%d as reply to %s …",
                    i + 1, len(thread_posts), parent_id,
                )
                time.sleep(_THREAD_POST_DELAY_SECONDS)
                try:
                    reply_result = self.threads.create_text_post(
                        thread_content, reply_to_id=parent_id,
                    )
                except Exception as exc:
                    self.logger.error(
                        "Thread post %d/%d failed: %s", i + 1, len(thread_posts), exc,
                    )
                    self.notifier.send(
                        "thread_post_failed",
                        f"Thread post {i+1}/{len(thread_posts)} failed for {target['id']}: {exc}",
                        SEVERITY_HIGH,
                    )
                    break
                reply_id = reply_result.get("id", "")
                thread_media_ids.append(reply_id)
                parent_id = reply_id  # chain replies
            else:
                self.logger.info(
                    "Thread complete: %d follow-up post(s) published.",
                    len(thread_media_ids),
                )

        # 4a. Append to post_history.json
        history_data = self.state.load_json("post_history.json")
        posts: list[dict] = history_data.setdefault("posts", [])

        post_record: dict = {
            "id": f"post_{now.strftime('%Y%m%d')}_{len(posts) + 1:03d}",
            "queue_id": target["id"],
            "threads_media_id": threads_media_id,
            "thread_media_ids": thread_media_ids if thread_media_ids else None,
            "content": content,
            "hashtag": target.get("hashtag", ""),
            "pattern": target.get("pattern", ""),
            "posted_at": posted_at,
            "quality_score": target.get("quality_score", 0),
            "category": target.get("category", ""),
            "debate_id": target.get("debate_id"),
            "debate_title": target.get("debate_title"),
            "metrics": {
                "views": 0,
                "likes": 0,
                "replies": 0,
                "reposts": 0,
                "quotes": 0,
                "last_fetched": None,
            },
        }
        posts.append(post_record)

        # 4b. Update queue item status
        target["status"] = "posted"
        target["posted_at"] = posted_at
        queue_data["last_updated"] = posted_at
        self.state.save_json("post_queue.json", queue_data)

        # 4c. Record post in safety counters
        self.safety.record_post()

        # 4d. Update daily_stats
        today_key = now.strftime("%Y-%m-%d")
        daily_stats: dict = history_data.setdefault("daily_stats", {})
        day_entry = daily_stats.setdefault(today_key, {"total_posts": 0})
        day_entry["total_posts"] += 1
        day_entry["last_post_at"] = posted_at

        history_data["last_updated"] = posted_at
        self.state.save_json("post_history.json", history_data)

        self.logger.info(
            "Post %s published successfully (threads_media_id=%s).",
            post_record["id"],
            threads_media_id,
        )

        # 5. Publish follow-up comment as a self-reply for コメント誘導型 posts
        follow_up_comment: str | None = target.get("follow_up_comment")
        if follow_up_comment and target.get("pattern") == "コメント誘導型":
            # Re-check emergency stop: can_post() was called only for the main post
            _sys_state = self.state.load_json("system_state.json")
            if _sys_state.get("emergency_stop"):
                self.logger.warning(
                    "Emergency stop active — skipping follow-up comment for %s.",
                    post_record["id"],
                )
            else:
                self.logger.info(
                    "Publishing follow-up comment for コメント誘導型 post %s …",
                    threads_media_id,
                )
                time.sleep(_THREAD_POST_DELAY_SECONDS)
                try:
                    self.threads.create_text_post(
                        follow_up_comment,
                        reply_to_id=threads_media_id,
                    )
                    self.logger.info("Follow-up comment posted.")
                except Exception as exc:
                    self.logger.warning(
                        "Follow-up comment failed for %s: %s", post_record["id"], exc
                    )

        # 6. Publish affiliate comment as a self-reply (if present)
        affiliate_comment: str | None = target.get("affiliate_comment")
        if affiliate_comment:
            affiliate_comment = self._ensure_pr_label(affiliate_comment)
            self.logger.info("Publishing affiliate comment as reply to %s …", threads_media_id)
            try:
                self.threads.create_text_post(
                    affiliate_comment,
                    reply_to_id=threads_media_id,
                )
                self.logger.info("Affiliate comment posted.")
            except Exception as exc:
                self.logger.error(
                    "Affiliate comment failed for %s: %s", threads_media_id, exc
                )
                self.notifier.send(
                    "affiliate_comment_failed",
                    f"Affiliate comment failed for {post_record['id']}: {exc}",
                    SEVERITY_HIGH,
                )

    # ------------------------------------------------------------------
    # PR label enforcement (ステマ規制対応)
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_pr_label(comment: str) -> str:
        """Ensure an affiliate comment starts with a PR disclosure label.

        Per Japanese stealth marketing regulations (ステマ規制), all
        promotional content must be clearly labelled.  This method
        prepends ``PR`` if no recognised label is present.

        Args:
            comment: The raw affiliate comment text.

        Returns:
            The comment with a PR label guaranteed at the start.
        """
        import re

        stripped = comment.strip()

        # Recognised PR label patterns (case-insensitive, full/half-width)
        pr_patterns = [
            r"^(?:【PR】|【ＰＲ】|\[PR\]|PR\s|ＰＲ\s|#PR\s|#PR$|広告|#広告|#ad\b|AD\s|【広告】)",
        ]

        for pattern in pr_patterns:
            if re.match(pattern, stripped, re.IGNORECASE):
                return stripped

        return f"PR\n{stripped}"
