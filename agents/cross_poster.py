"""CrossPoster agent — manages cross-account quote reposts.

Scans high-engagement posts across all registered accounts and creates
quote reposts from a different account's perspective to drive cross-audience
discovery.
"""

from __future__ import annotations

import datetime
import random
from pathlib import Path
from typing import Any

from agents.base_agent import BaseAgent
from core.account_context import AccountContext
from core.constants import JST
from core.logger import get_logger
from services.claude_client import ClaudeClient
from services.threads_api import ThreadsAPIClient

logger = get_logger("cross_poster")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CrossPosterAgent(BaseAgent):
    """Create quote reposts between accounts to boost cross-audience reach.

    This agent runs under a specific account context (the "quoting" account)
    and scans other accounts' post histories for high-engagement posts worth
    quoting.

    Flow:
        1. Load cross_posting.yaml config.
        2. List all registered accounts, exclude self.
        3. For each other account, load its post_history and filter for
           buzz-level posts.
        4. Check cross_post_state.json to skip already-quoted posts and
           respect rate limits.
        5. Generate a persona-appropriate quote comment via Claude.
        6. Post the quote repost via Threads API.
        7. Record in cross_post_state.json.
    """

    def __init__(self, ctx: AccountContext | None = None) -> None:
        super().__init__("cross_poster", ctx=ctx)
        self.claude_client = ClaudeClient()
        self.threads = ThreadsAPIClient()

        # Load cross-posting config (always from project root)
        config_path = _PROJECT_ROOT / "config" / "cross_posting.yaml"
        if config_path.exists():
            import yaml
            with open(config_path, encoding="utf-8") as f:
                self._config: dict[str, Any] = yaml.safe_load(f) or {}
        else:
            self._config = {}

        if not self._config.get("enabled", False):
            self.logger.info("Cross-posting is disabled.")

        criteria = self._config.get("candidate_criteria", {})
        self._min_views: int = criteria.get("min_views", 200)
        self._min_engagement: float = criteria.get("min_engagement_rate", 0.03)
        self._max_age_hours: int = criteria.get("max_age_hours", 72)
        self._min_age_hours: int = criteria.get("min_age_hours", 6)

        limits = self._config.get("limits", {})
        self._max_daily: int = limits.get("max_cross_posts_per_day", 3)
        self._min_interval_hours: int = limits.get("min_interval_hours", 24)
        self._max_pair_weekly: int = limits.get("max_same_pair_per_week", 2)
        self._cross_post_rate: float = limits.get("cross_post_rate", 0.15)

        comment_cfg = self._config.get("comment", {})
        self._min_comment_len: int = comment_cfg.get("min_length", 30)
        self._max_comment_len: int = comment_cfg.get("max_length", 200)

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def execute(self) -> None:
        """Scan other accounts and create cross-account quote reposts."""
        if not self._config.get("enabled", False):
            self.logger.info("Cross-posting disabled. Skipping.")
            return

        now = datetime.datetime.now(JST)

        # Check active hours
        schedule = self._config.get("schedule", {})
        active_hours = schedule.get("active_hours", {})
        start_hour = active_hours.get("start", 8)
        end_hour = active_hours.get("end", 22)
        if not (start_hour <= now.hour < end_hour):
            self.logger.info("Outside active hours (%d-%d). Skipping.", start_hour, end_hour)
            return

        # Load state
        cp_state = self.state.load_json("cross_post_state.json")

        # Check daily limit
        today_str = now.strftime("%Y-%m-%d")
        daily_counts: dict[str, int] = cp_state.get("daily_counts", {})
        today_count = daily_counts.get(today_str, 0)
        if today_count >= self._max_daily:
            self.logger.info("Daily cross-post limit reached (%d). Skipping.", self._max_daily)
            return

        # Get other accounts
        other_accounts = self._get_other_accounts()
        if not other_accounts:
            self.logger.info("No other accounts found. Skipping.")
            return

        # Find candidates across other accounts
        candidates = self._find_candidates(other_accounts, cp_state, now)
        if not candidates:
            self.logger.info("No cross-post candidates found.")
            return

        # Apply cross_post_rate to select subset
        selected = [c for c in candidates if random.random() < self._cross_post_rate]
        if not selected:
            selected = candidates[:1]  # At least try one if we have candidates

        # Limit to remaining daily quota
        remaining = self._max_daily - today_count
        selected = selected[:remaining]

        posted = 0
        for candidate in selected:
            success = self._create_cross_post(candidate, cp_state, now)
            if success:
                posted += 1
                today_count += 1

        if posted > 0:
            # Update daily count and save
            daily_counts[today_str] = today_count
            cp_state["daily_counts"] = daily_counts
            cp_state["last_updated"] = now.isoformat()
            self.state.save_json("cross_post_state.json", cp_state)
            self.logger.info("Cross-posted %d quote repost(s).", posted)
        else:
            self.logger.info("No cross-posts created this run.")

    # ------------------------------------------------------------------
    # Account discovery
    # ------------------------------------------------------------------

    def _get_other_accounts(self) -> list[str]:
        """Return account IDs excluding this agent's account."""
        accounts_dir = _PROJECT_ROOT / "accounts"
        if not accounts_dir.exists():
            return []
        all_accounts = sorted(
            d.name
            for d in accounts_dir.iterdir()
            if d.is_dir() and not d.name.startswith("_")
        )
        return [a for a in all_accounts if a != self.ctx.account_id]

    # ------------------------------------------------------------------
    # Candidate selection
    # ------------------------------------------------------------------

    def _find_candidates(
        self,
        other_accounts: list[str],
        cp_state: dict[str, Any],
        now: datetime.datetime,
    ) -> list[dict[str, Any]]:
        """Scan other accounts' post histories for high-engagement posts."""
        candidates: list[dict[str, Any]] = []
        cross_posts: list[dict[str, Any]] = cp_state.get("cross_posts", [])
        already_quoted: set[str] = {
            cp["source_post_id"] for cp in cross_posts
        }

        for account_id in other_accounts:
            try:
                other_ctx = AccountContext(account_id)
            except ValueError:
                continue

            other_sm = other_ctx.get_state_manager()
            history = other_sm.load_json("post_history.json")
            posts: list[dict[str, Any]] = history.get("posts", [])

            for post in posts:
                post_id = post.get("id", "")
                if post_id in already_quoted:
                    continue

                # Check age constraints
                try:
                    posted_at = datetime.datetime.fromisoformat(post["posted_at"])
                except (KeyError, ValueError, TypeError):
                    continue

                age_hours = (now - posted_at).total_seconds() / 3600
                if age_hours < self._min_age_hours or age_hours > self._max_age_hours:
                    continue

                # Check engagement metrics
                metrics = post.get("metrics", {})
                views = metrics.get("views", 0)
                if views < self._min_views:
                    continue

                likes = metrics.get("likes", 0)
                replies = metrics.get("replies", 0)
                reposts = metrics.get("reposts", 0)
                engagement_rate = (likes + replies + reposts) / max(views, 1)
                if engagement_rate < self._min_engagement:
                    continue

                # Check pair interval
                pair_key = f"{account_id}->{self.ctx.account_id}"
                if not self._check_pair_limits(pair_key, cp_state, now):
                    continue

                candidates.append({
                    "source_account": account_id,
                    "source_post_id": post_id,
                    "source_media_id": post.get("media_id", ""),
                    "source_content": post.get("content", ""),
                    "source_category": post.get("category", ""),
                    "engagement_rate": engagement_rate,
                    "views": views,
                    "pair_key": pair_key,
                })

        # Sort by engagement rate descending
        candidates.sort(key=lambda c: c["engagement_rate"], reverse=True)
        return candidates

    def _check_pair_limits(
        self,
        pair_key: str,
        cp_state: dict[str, Any],
        now: datetime.datetime,
    ) -> bool:
        """Check interval and weekly limits for a specific account pair."""
        cross_posts: list[dict[str, Any]] = cp_state.get("cross_posts", [])

        pair_posts = [
            cp for cp in cross_posts if cp.get("pair_key") == pair_key
        ]

        if not pair_posts:
            return True

        # Check interval
        latest = max(
            pair_posts,
            key=lambda cp: cp.get("created_at", ""),
        )
        try:
            latest_at = datetime.datetime.fromisoformat(latest["created_at"])
            hours_since = (now - latest_at).total_seconds() / 3600
            if hours_since < self._min_interval_hours:
                return False
        except (KeyError, ValueError):
            pass

        # Check weekly limit
        week_ago = now - datetime.timedelta(days=7)
        weekly_count = sum(
            1 for cp in pair_posts
            if cp.get("created_at", "") >= week_ago.isoformat()
        )
        if weekly_count >= self._max_pair_weekly:
            return False

        return True

    # ------------------------------------------------------------------
    # Cross-post creation
    # ------------------------------------------------------------------

    def _create_cross_post(
        self,
        candidate: dict[str, Any],
        cp_state: dict[str, Any],
        now: datetime.datetime,
    ) -> bool:
        """Generate and publish a quote repost for a candidate."""
        source_content = candidate["source_content"]
        source_account = candidate["source_account"]

        if not source_content or not candidate.get("source_media_id"):
            return False

        # Load own persona for comment generation
        try:
            tone = self._load_yaml("config/tone.yaml")
        except FileNotFoundError:
            self.logger.warning("tone.yaml not found. Skipping cross-post.")
            return False

        persona = tone.get("persona", {})
        persona_name = persona.get("name", self.ctx.account_id)
        style = tone.get("style", {})
        first_person = style.get("first_person", "")

        # Generate quote comment via Claude
        prompt = (
            f"あなたは「{persona_name}」として、"
            f"別のアカウントの投稿を引用リポストするコメントを書きます。\n\n"
            f"## あなたのペルソナ\n"
            f"- 一人称: {first_person or '使わない'}\n"
            f"- 口調: {style.get('tone', 'カジュアル')}\n"
            f"- 語尾例: {', '.join(style.get('use_endings', [])[:3])}\n\n"
            f"## 引用元の投稿内容\n{source_content}\n\n"
            f"## ルール\n"
            f"- {self._min_comment_len}〜{self._max_comment_len}文字\n"
            f"- あなた独自の視点・補足を加える（単なる同意は不可）\n"
            f"- 製品名・ブランド名は出さない\n"
            f"- 薬機法違反表現は使わない\n"
            f"- 自然な引用コメントのみ出力（説明文やメタ情報は不要）\n"
        )

        try:
            comment = self.claude_client.generate_post(prompt)
        except Exception as exc:
            self.logger.error("Claude comment generation failed: %s", exc)
            return False

        if not comment:
            return False

        # Clean up: take first paragraph only
        comment = comment.strip().split("\n\n")[0].strip()

        # Length check
        if len(comment) < self._min_comment_len:
            self.logger.warning("Generated comment too short (%d chars). Skipping.", len(comment))
            return False
        if len(comment) > self._max_comment_len:
            comment = comment[:self._max_comment_len]

        # Post via Threads API (quote repost)
        try:
            result = self.threads.create_text_post(
                text=comment,
                quote_post_id=candidate["source_media_id"],
            )
        except Exception as exc:
            self.logger.error("Cross-post failed: %s", exc)
            return False

        media_id = result.get("id", "")

        # Record in state
        record = {
            "source_account": source_account,
            "source_post_id": candidate["source_post_id"],
            "source_media_id": candidate["source_media_id"],
            "quoting_account": self.ctx.account_id,
            "quote_media_id": media_id,
            "comment": comment,
            "pair_key": candidate["pair_key"],
            "created_at": now.isoformat(),
        }

        cross_posts: list[dict[str, Any]] = cp_state.setdefault("cross_posts", [])
        cross_posts.append(record)

        self.logger.info(
            "Cross-posted: %s quoted %s's post %s (media=%s)",
            self.ctx.account_id,
            source_account,
            candidate["source_post_id"],
            media_id,
        )
        return True
