"""Replier agent — automatically replies to follower comments on posts."""

from __future__ import annotations

import datetime
import random
from pathlib import Path
from typing import Any
from agents.base_agent import BaseAgent
from core.constants import JST
from core.notifier import Notifier, SEVERITY_MEDIUM, SEVERITY_HIGH, SEVERITY_CRITICAL
from core.quality_gate import QualityGate
from services.claude_client import ClaudeClient
from services.threads_api import ThreadsAPIClient

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Compliance risk keywords for comment monitoring (10.4)
_COMPLIANCE_KEYWORDS: dict[str, list[str]] = {
    "critical": ["通報", "訴訟", "消費者庁", "弁護士"],
    "high": ["薬機法", "景表法", "違法", "違反"],
    "medium": [
        "ステマ", "案件", "PR表記ない", "ヤラセ",
        "嘘", "デマ", "エビデンスは", "ソースは",
    ],
}


class ReplierAgent(BaseAgent):
    """Fetch follower comments on recent posts and reply as '@seibun_love'.

    This agent boosts engagement by responding to follower comments in
    the persona's voice.  A subset of replies may include a subtle
    profile link CTA to drive affiliate traffic.

    Safety constraints:
    - Only replies to a configurable percentage of comments (avoids bot feel)
    - Enforces a random delay window before replying (human-like timing)
    - Limits daily reply count
    - Skips own comments (e.g. affiliate replies)
    - Limits replies per unique user per day
    - Runs NG-word check on every generated reply
    """

    def __init__(self) -> None:
        super().__init__("replier")
        self.threads = ThreadsAPIClient()
        self.claude_client = ClaudeClient()
        self.quality_gate = QualityGate()
        self.notifier = Notifier()

        # Load configuration
        self.settings = self._load_yaml("config/settings.yaml")
        replier_cfg: dict[str, Any] = self.settings.get("replier", {})

        self.reply_rate: float = replier_cfg.get("reply_rate", 0.65)
        self.min_reply_delay: int = replier_cfg.get("min_reply_delay_seconds", 300)
        self.max_reply_delay: int = replier_cfg.get("max_reply_delay_seconds", 7200)
        self.max_replies_per_run: int = replier_cfg.get("max_replies_per_run", 5)
        self.max_daily_replies: int = replier_cfg.get("max_daily_replies", 20)
        self.profile_cta_rate: float = replier_cfg.get("profile_cta_rate", 0.3)
        self.scan_hours: int = replier_cfg.get("scan_hours", 48)
        self.skip_own_replies: bool = replier_cfg.get("skip_own_replies", True)
        self.max_replies_per_user_per_day: int = replier_cfg.get(
            "max_replies_per_user_per_day", 2
        )

        # Load prompt template
        self.system_prompt: str = self._load_text("prompts/replier.md")

        self.logger.info(
            "ReplierAgent initialised (reply_rate=%.0f%%, max_daily=%d)",
            self.reply_rate * 100,
            self.max_daily_replies,
        )

    # ==================================================================
    # Core logic
    # ==================================================================

    def execute(self) -> None:
        """Scan recent posts for new comments and reply to eligible ones."""
        now = datetime.datetime.now(JST)

        # 1. Check daily reply budget
        reply_state = self.state.load_json("reply_state.json")
        today_key = now.strftime("%Y-%m-%d")
        daily_count = reply_state.get("daily_reply_count", {}).get(today_key, 0)

        if daily_count >= self.max_daily_replies:
            self.logger.info(
                "Daily reply limit reached (%d/%d). Skipping.",
                daily_count,
                self.max_daily_replies,
            )
            return

        remaining_budget = min(
            self.max_replies_per_run,
            self.max_daily_replies - daily_count,
        )

        # 2. Get recent posts (within scan window)
        history_data = self.state.load_json("post_history.json")
        posts: list[dict[str, Any]] = history_data.get("posts", [])
        cutoff = now - datetime.timedelta(hours=self.scan_hours)

        recent_posts = []
        for post in posts:
            try:
                posted_at = datetime.datetime.fromisoformat(post["posted_at"])
                if posted_at >= cutoff and post.get("threads_media_id"):
                    recent_posts.append(post)
            except (KeyError, ValueError):
                continue

        if not recent_posts:
            self.logger.info("No recent posts to scan for comments.")
            return

        self.logger.info("Scanning %d recent posts for comments.", len(recent_posts))

        # 3. Collect all unprocessed comments
        processed: dict[str, dict] = reply_state.get("processed_comments", {})
        own_username = self._get_own_username()
        if self.skip_own_replies and not own_username:
            self.logger.warning(
                "Could not resolve own username; self-reply skip is disabled for this run."
            )
        candidates: list[dict[str, Any]] = []

        for post in recent_posts:
            media_id = post["threads_media_id"]
            try:
                replies = self.threads.get_post_replies(media_id)
            except Exception as exc:
                self.logger.warning(
                    "Failed to fetch replies for %s: %s", media_id, exc
                )
                continue

            for reply in replies:
                comment_id = reply.get("id", "")
                if not comment_id or comment_id in processed:
                    continue

                # Skip own comments (e.g. affiliate replies)
                if self.skip_own_replies and own_username:
                    if reply.get("username", "").lower() == own_username.lower():
                        # Mark as processed so we don't check again
                        processed[comment_id] = {
                            "post_id": post.get("id", ""),
                            "action": "skipped_own",
                            "replied_at": now.isoformat(),
                        }
                        continue

                candidates.append({
                    "comment_id": comment_id,
                    "comment_text": reply.get("text", ""),
                    "comment_username": reply.get("username", ""),
                    "comment_timestamp": reply.get("timestamp", ""),
                    "post_id": post.get("id", ""),
                    "post_media_id": media_id,
                    "post_content": post.get("content", ""),
                    "post_metrics": post.get("metrics", {}),
                })

        if not candidates:
            self.logger.info("No new comments to process.")
            self._save_reply_state(reply_state, now)
            return

        self.logger.info("Found %d new comment(s) to evaluate.", len(candidates))

        # Compliance risk scanning (10.4)
        self._scan_compliance_risk(candidates)

        # 4. Select which comments to reply to
        random.shuffle(candidates)
        reply_targets: list[dict[str, Any]] = []
        user_reply_counts: dict[str, int] = self._get_today_user_counts(
            processed, today_key
        )

        for candidate in candidates:
            if len(reply_targets) >= remaining_budget:
                break

            username = candidate["comment_username"]

            # Check per-user daily limit
            if user_reply_counts.get(username, 0) >= self.max_replies_per_user_per_day:
                processed[candidate["comment_id"]] = {
                    "post_id": candidate["post_id"],
                    "action": "skipped_user_limit",
                    "replied_at": now.isoformat(),
                }
                continue

            # Check delay window (comment must be within reply window)
            try:
                comment_time = datetime.datetime.fromisoformat(
                    candidate["comment_timestamp"]
                )
                age_seconds = (now - comment_time).total_seconds()
                if age_seconds < self.min_reply_delay:
                    # Too fresh — skip for now, will pick up next run
                    continue
                if age_seconds > self.max_reply_delay:
                    # Too old — replying now would look unnatural
                    processed[candidate["comment_id"]] = {
                        "post_id": candidate["post_id"],
                        "action": "skipped_too_old",
                        "replied_at": now.isoformat(),
                    }
                    continue
            except (ValueError, TypeError):
                pass  # If timestamp is invalid, proceed anyway

            # Apply reply_rate: randomly decide whether to reply or just skip
            if random.random() > self.reply_rate:
                processed[candidate["comment_id"]] = {
                    "post_id": candidate["post_id"],
                    "action": "skipped_random",
                    "replied_at": now.isoformat(),
                }
                continue

            reply_targets.append(candidate)
            user_reply_counts[username] = user_reply_counts.get(username, 0) + 1

        if not reply_targets:
            self.logger.info("No comments selected for reply this run.")
            self._save_reply_state(reply_state, now)
            return

        # 5. Generate and post replies
        replies_posted = 0

        for target in reply_targets:
            try:
                reply_text = self._generate_reply(target)

                if not reply_text:
                    processed[target["comment_id"]] = {
                        "post_id": target["post_id"],
                        "action": "skipped_generation_failed",
                        "replied_at": now.isoformat(),
                    }
                    continue

                # NG word check
                ng_result = self.quality_gate.check_ng_words(reply_text)
                if ng_result:
                    self.logger.warning(
                        "Reply NG word detected: %s. Skipping.", ng_result
                    )
                    processed[target["comment_id"]] = {
                        "post_id": target["post_id"],
                        "action": "skipped_ng_word",
                        "replied_at": now.isoformat(),
                    }
                    continue

                # Post the reply
                result = self.threads.create_text_post(
                    reply_text,
                    reply_to_id=target["comment_id"],
                )

                # Record success
                include_cta = "プロフ" in reply_text
                processed[target["comment_id"]] = {
                    "post_id": target["post_id"],
                    "action": "replied",
                    "replied_at": now.isoformat(),
                    "reply_media_id": result.get("id", ""),
                    "reply_text": reply_text,
                    "had_profile_cta": include_cta,
                    "reply_to_username": target["comment_username"],
                }
                replies_posted += 1

                self.logger.info(
                    "Replied to @%s on post %s (cta=%s)",
                    target["comment_username"],
                    target["post_id"],
                    include_cta,
                )

            except Exception as exc:
                self.logger.error(
                    "Failed to reply to comment %s: %s",
                    target["comment_id"],
                    exc,
                )
                processed[target["comment_id"]] = {
                    "post_id": target["post_id"],
                    "action": "error",
                    "replied_at": now.isoformat(),
                    "error": str(exc),
                }

        # 6. Update state
        reply_state["processed_comments"] = processed
        daily_counts = reply_state.setdefault("daily_reply_count", {})
        daily_counts[today_key] = daily_count + replies_posted
        self._save_reply_state(reply_state, now)

        # 7. Detect questions from comments and add to research pool
        question_cfg = self.settings.get("replier", {}).get("question_detection", {})
        if question_cfg.get("enabled", True):
            self._detect_and_pool_questions(candidates, processed)

        self.logger.info(
            "Replier run complete — %d/%d replies posted (daily total: %d/%d).",
            replies_posted,
            len(reply_targets),
            daily_count + replies_posted,
            self.max_daily_replies,
        )

        # 8. Outbound replies (engage with external posts)
        outbound_cfg = self.settings.get("replier", {}).get("outbound", {})
        if outbound_cfg.get("enabled", False):
            outbound_posted = self._execute_outbound_replies(outbound_cfg)
            if outbound_posted:
                self.logger.info("Outbound replies: %d posted.", outbound_posted)

    # ==================================================================
    # Reply generation
    # ==================================================================

    def _generate_reply(self, target: dict[str, Any]) -> str | None:
        """Generate a reply to a follower comment using Claude.

        Args:
            target: Dict containing comment_text, comment_username,
                post_content, etc.

        Returns:
            The reply text, or None if generation failed.
        """
        # Boost CTA rate for buzz posts (1h stage: views 500+ & engagement 5%+)
        cta_rate = self.profile_cta_rate
        post_metrics = target.get("post_metrics", {})
        stage_1h = post_metrics.get("stages", {}).get("1h", {})
        if stage_1h:
            views = stage_1h.get("views", 0)
            eng = stage_1h.get("likes", 0) + stage_1h.get("replies", 0)
            if views >= 500 and views > 0 and (eng / views * 100) >= 5:
                cta_rate = min(cta_rate * 3, 0.9)  # 3x boost, cap at 90%

        include_cta = random.random() < cta_rate

        cta_instruction = ""
        if include_cta:
            cta_instruction = (
                "\n\n【プロフ誘導】この返信の末尾に、自然な形でプロフィールへの"
                "誘導を1文追加してください。"
                "必ず文頭に「【PR】」を付けてください（ステマ規制対応）。"
                "例: 「【PR】プロフにまとめてるから見てみて→」"
            )

        prompt = (
            f"## 元の投稿\n{target['post_content']}\n\n"
            f"## フォロワーのコメント\n"
            f"@{target['comment_username']}: {target['comment_text']}\n\n"
            f"## 指示\n"
            f"上記のコメントに「成分だけ、愛してる。」のアカウントとして返信してください。\n"
            f"- 50〜150文字\n"
            f"- 相手のコメント内容に具体的に触れること\n"
            f"- 返信テキストのみを出力（余計な説明不要）"
            f"{cta_instruction}"
        )

        try:
            reply_text = self.claude_client.generate_post(
                prompt=prompt,
                system_prompt=self.system_prompt,
            )
            reply_text = reply_text.strip()

            # Basic length validation
            if len(reply_text) < 10 or len(reply_text) > 200:
                self.logger.debug(
                    "Reply length %d out of range. Discarding.", len(reply_text)
                )
                return None

            return reply_text

        except Exception as exc:
            self.logger.error("Reply generation failed: %s", exc)
            return None

    # ==================================================================
    # Helpers
    # ==================================================================

    _own_username_cache: str | None = None
    _own_username_fetched_at: float = 0.0
    _OWN_USERNAME_TTL: float = 3600.0  # Retry after 1 hour on failure

    def _get_own_username(self) -> str | None:
        """Return the authenticated account's username (cached with TTL).

        On success the result is cached indefinitely. On failure the
        result (``None``) is cached for ``_OWN_USERNAME_TTL`` seconds so
        that a transient network error does not permanently disable the
        self-reply skip mechanism.
        """
        import time

        now = time.monotonic()
        if self._own_username_cache is not None:
            return self._own_username_cache
        if now - self._own_username_fetched_at < self._OWN_USERNAME_TTL:
            return None  # Still within cooldown after a previous failure

        try:
            profile = self.threads.get_user_profile()
            self._own_username_cache = profile.get("username")
        except Exception as exc:
            self.logger.warning("Could not fetch own username: %s", exc)
            self._own_username_cache = None
        self._own_username_fetched_at = now
        return self._own_username_cache

    def _get_today_user_counts(
        self, processed: dict[str, dict], today_key: str
    ) -> dict[str, int]:
        """Count how many replies were sent to each user today."""
        counts: dict[str, int] = {}
        for _comment_id, record in processed.items():
            if record.get("action") != "replied":
                continue
            replied_at = record.get("replied_at", "")
            if replied_at.startswith(today_key):
                username = record.get("reply_to_username", "")
                if username:
                    counts[username] = counts.get(username, 0) + 1
        return counts

    def _save_reply_state(
        self, reply_state: dict[str, Any], now: datetime.datetime
    ) -> None:
        """Persist reply_state.json with cleanup of old entries.

        Privacy protection: usernames are stripped from entries older than
        24 hours to minimise personal data retention.
        """
        # Cleanup: remove processed entries older than 7 days
        cutoff = (now - datetime.timedelta(days=7)).isoformat()
        username_cutoff = (now - datetime.timedelta(days=1)).isoformat()
        processed = reply_state.get("processed_comments", {})
        to_remove = [
            cid for cid, record in processed.items()
            if record.get("replied_at", "") < cutoff
        ]
        for cid in to_remove:
            del processed[cid]

        # Strip usernames from entries older than 24h (privacy protection)
        for _cid, record in processed.items():
            if record.get("replied_at", "") < username_cutoff:
                record.pop("reply_to_username", None)

        # Cleanup: remove daily counts older than 7 days
        daily_counts = reply_state.get("daily_reply_count", {})
        cutoff_date = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        old_keys = [k for k in daily_counts if k < cutoff_date]
        for k in old_keys:
            del daily_counts[k]

        reply_state["last_updated"] = now.isoformat()
        self.state.save_json("reply_state.json", reply_state)

    def _detect_and_pool_questions(
        self,
        candidates: list[dict[str, Any]],
        processed: dict[str, dict],
    ) -> None:
        """Scan comment candidates for questions and add to research_pool."""
        question_markers = ["？", "?", "教えて", "どう", "おすすめ", "どっち", "何が"]

        questions_found: list[dict[str, Any]] = []
        for candidate in candidates:
            text = candidate.get("comment_text", "")
            if len(text) >= 10 and any(m in text for m in question_markers):
                questions_found.append(candidate)

        if not questions_found:
            return

        self.logger.info("Detected %d question(s) from comments.", len(questions_found))

        pool_data = self.state.load_json("research_pool.json")
        items: list[dict[str, Any]] = pool_data.get("items", [])
        now = datetime.datetime.now(JST)
        date_str = now.strftime("%Y%m%d")

        existing_questions: set[str] = {
            item.get("question_text", "")
            for item in items
            if item.get("source") == "follower_question"
        }

        added = 0
        for idx, q in enumerate(questions_found[:5], start=1):
            q_text = q["comment_text"]
            if q_text in existing_questions:
                continue

            # NOTE: username is intentionally NOT stored (privacy protection).
            items.append({
                "id": f"fq_{date_str}_{idx:03d}",
                "source": "follower_question",
                "source_url": "",
                "topic": f"フォロワー質問: {q_text[:50]}",
                "summary": q_text,
                "question_text": q_text,
                "keywords": [],
                "category": "skincare_knowledge",
                "collected_at": now.isoformat(),
                "used": False,
                "priority": 8,
            })
            existing_questions.add(q_text)
            added += 1

        if added:
            pool_data["items"] = items
            pool_data["last_updated"] = now.isoformat()
            self.state.save_json("research_pool.json", pool_data)
            self.logger.info("Added %d follower question(s) to research pool.", added)

    # ==================================================================
    # Compliance monitoring (10.4)
    # ==================================================================

    def _scan_compliance_risk(self, candidates: list[dict[str, Any]]) -> None:
        """Scan comment candidates for compliance risk keywords.

        Detects keywords indicating legal complaints, regulatory concerns,
        or trust issues, and sends Telegram alerts via Notifier.
        """
        for candidate in candidates:
            text = candidate.get("comment_text", "")
            if not text:
                continue

            post_id = candidate.get("post_id", "unknown")

            # Check each severity level (highest first — order matters)
            for severity_key in ("critical", "high", "medium"):
                keywords = _COMPLIANCE_KEYWORDS[severity_key]
                matched = [kw for kw in keywords if kw in text]
                if not matched:
                    continue

                # Map to notifier severity
                severity_map = {
                    "critical": SEVERITY_CRITICAL,
                    "high": SEVERITY_HIGH,
                    "medium": SEVERITY_MEDIUM,
                }
                severity = severity_map[severity_key]

                msg = (
                    f"[COMPLIANCE] 投稿 {post_id} にコンプラリスクコメント検知\n"
                    f"キーワード: {', '.join(matched)}\n"
                    f"コメント: {text[:100]}"
                )

                self.notifier.send("compliance_risk", msg, severity)
                self.logger.warning(
                    "Compliance risk detected on post %s: %s (severity=%s)",
                    post_id,
                    matched,
                    severity_key,
                )

                # Flag the post in post_history
                self._flag_compliance_risk(post_id, matched, severity_key)

                # Only report the highest severity match per comment
                break

    def _flag_compliance_risk(
        self, post_id: str, keywords: list[str], severity: str
    ) -> None:
        """Add a compliance_risk flag to the post in post_history.json."""
        history = self.state.load_json("post_history.json")
        for post in history.get("posts", []):
            if post.get("id") == post_id:
                risks = post.setdefault("compliance_risks", [])
                risks.append({
                    "detected_at": datetime.datetime.now(JST).isoformat(),
                    "keywords": keywords,
                    "severity": severity,
                })
                self.state.save_json("post_history.json", history)
                break

    # ==================================================================
    # Outbound replies (4-3)
    # ==================================================================

    def _execute_outbound_replies(self, cfg: dict[str, Any]) -> int:
        """Process queued outbound reply targets.

        Reads ``data/state/outbound_queue.json`` for pending targets,
        generates contextual replies, and posts them.  Targets are added
        manually via ``scripts/add_outbound.py``.

        Returns:
            Number of outbound replies posted.
        """
        now = datetime.datetime.now(JST)
        today_key = now.strftime("%Y-%m-%d")

        queue_data = self.state.load_json("outbound_queue.json")
        targets: list[dict[str, Any]] = queue_data.get("targets", [])

        if not targets:
            return 0

        # Check daily limit
        daily_outbound = queue_data.get("daily_count", {}).get(today_key, 0)
        max_daily = cfg.get("max_daily", 5)
        max_per_run = cfg.get("max_per_run", 2)
        remaining = min(max_per_run, max_daily - daily_outbound)

        if remaining <= 0:
            self.logger.info("Outbound daily limit reached (%d/%d).", daily_outbound, max_daily)
            return 0

        cta_rate = cfg.get("profile_cta_rate", 0.2)
        posted = 0

        for target in targets:
            if posted >= remaining:
                break
            if target.get("status") != "pending":
                continue

            media_id = target.get("target_media_id", "")
            context = target.get("context", "")
            if not media_id:
                target["status"] = "skipped_no_id"
                continue

            try:
                reply_text = self._generate_outbound_reply(context, cta_rate)
                if not reply_text:
                    target["status"] = "skipped_generation_failed"
                    continue

                # NG word check
                ng_result = self.quality_gate.check_ng_words(reply_text)
                if ng_result:
                    self.logger.warning("Outbound reply NG word: %s", ng_result)
                    target["status"] = "skipped_ng_word"
                    continue

                result = self.threads.create_text_post(
                    reply_text, reply_to_id=media_id
                )

                target["status"] = "posted"
                target["posted_at"] = now.isoformat()
                target["reply_media_id"] = result.get("id", "")
                target["reply_text"] = reply_text
                posted += 1

                self.logger.info(
                    "Outbound reply posted to %s", media_id
                )

            except Exception as exc:
                self.logger.error("Outbound reply failed for %s: %s", media_id, exc)
                target["status"] = "error"
                target["error"] = str(exc)

        # Update state
        daily_counts = queue_data.setdefault("daily_count", {})
        daily_counts[today_key] = daily_outbound + posted
        queue_data["last_updated"] = now.isoformat()
        self.state.save_json("outbound_queue.json", queue_data)

        return posted

    def _generate_outbound_reply(
        self, context: str, cta_rate: float
    ) -> str | None:
        """Generate a reply for an external post.

        Args:
            context: Description or content of the target post.
            cta_rate: Probability of including a profile CTA.

        Returns:
            Reply text or None on failure.
        """
        prompt = (
            "## 他ユーザーの投稿\n"
            f"{context}\n\n"
            "## 指示\n"
            "上記の投稿に成分知識を活かした有益なコメントを書いてください。\n"
            "- 50〜150文字\n"
            "- 上から目線にならない。フレンドリーに\n"
            "- 自分の知識を押し付けず、役立つ情報を1つ添える\n"
            "- 宣伝・誘導は一切しない（アウトバウンドでは純粋に価値提供のみ）\n"
            "- コメントテキストのみを出力"
        )

        try:
            text = self.claude_client.generate_post(
                prompt=prompt, system_prompt=self.system_prompt
            )
            text = text.strip()
            if len(text) < 10 or len(text) > 200:
                return None
            return text
        except Exception as exc:
            self.logger.error("Outbound reply generation failed: %s", exc)
            return None

    @staticmethod
    def _load_yaml(relative_path: str) -> dict[str, Any]:
        """Load a YAML file relative to the project root."""
        import yaml

        full_path = _PROJECT_ROOT / relative_path
        with open(full_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _load_text(relative_path: str) -> str:
        """Load a text file relative to the project root."""
        full_path = _PROJECT_ROOT / relative_path
        with open(full_path, encoding="utf-8") as f:
            return f.read()
