"""Fetcher agent — retrieves post insights from Threads and updates metrics.

Also handles retroactive affiliate comment injection for high-performing
(buzz) posts, using a product catalog for targeted recommendations.
"""

from __future__ import annotations

import datetime
import random
from pathlib import Path
from typing import Any
import yaml

from agents.base_agent import BaseAgent
from core.constants import JST
from core.notifier import Notifier, SEVERITY_MEDIUM
from services.claude_client import ClaudeClient
from services.threads_api import (
    AuthenticationError,
    RateLimitError,
    ThreadsAPIClient,
    ThreadsAPIError,
)

# 2-stage measurement (V2.1) — 1h stage removed because the fetcher runs
# every 6 hours, making a 1-hour snapshot structurally impossible.
_MEASUREMENT_STAGES: list[tuple[str, int, str | None]] = [
    ("6h", 6, "24h"),
    ("24h", 24, None),  # terminal
]
_STAGE_NAMES = [s[0] for s in _MEASUREMENT_STAGES]
_STAGE_MIN_AGE = {s[0]: s[1] for s in _MEASUREMENT_STAGES}
_STAGE_NEXT = {s[0]: s[2] for s in _MEASUREMENT_STAGES}

# Max consecutive API failures before marking a post as unfetchable
_MAX_FETCH_FAILURES = 3

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ANALYTICS_DIR = _PROJECT_ROOT / "data" / "analytics"


class FetcherAgent(BaseAgent):
    """Fetch Threads Insights for published posts and persist the metrics.

    Targets posts that are either:
    * older than 6 hours and have never been fetched, or
    * older than 6 hours and last fetched more than 12 hours ago.

    Retrieved metrics are written to both ``post_history.json`` (per-post
    ``metrics`` block) and ``data/analytics/performance.json``.

    Additionally, performs **retroactive affiliate injection**: when a post
    exceeds the buzz thresholds and has no affiliate comment yet, one is
    generated via Claude and posted as a self-reply.  Product selection is
    driven by ``config/affiliate_products.yaml``.
    """

    def __init__(self) -> None:
        super().__init__("fetcher")
        self.threads = ThreadsAPIClient()
        self.claude_client = ClaudeClient()
        self.notifier = Notifier()

        # Load affiliate config
        config_path = _PROJECT_ROOT / "config" / "settings.yaml"
        with open(config_path, encoding="utf-8") as f:
            settings = yaml.safe_load(f)

        aff_cfg: dict[str, Any] = settings.get("affiliate", {})
        self.affiliate_mode: str = aff_cfg.get("mode", "retroactive")
        self.buzz_views: int = aff_cfg.get("buzz_views_threshold", 500)
        self.buzz_engagement: float = aff_cfg.get("buzz_engagement_threshold", 0.05)
        self.max_affiliate_per_day: int = aff_cfg.get("max_affiliate_per_day", 5)
        self.affiliate_cooldown_hours: int = aff_cfg.get("cooldown_hours", 6)

        # Load product catalog
        self._product_catalog: list[dict[str, Any]] | None = None
        self._matching_cfg: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def execute(self) -> None:
        """Scan post history and fetch fresh insights for eligible posts."""
        now = datetime.datetime.now(JST)

        # 1. Identify posts that need metrics
        history_data = self.state.load_json("post_history.json")
        posts: list[dict] = history_data.get("posts", [])

        # Load min_hours_after_post from settings (default: 6)
        first_stage = _STAGE_NAMES[0]
        min_age = _STAGE_MIN_AGE[first_stage]

        eligible: list[tuple[dict, str]] = []  # (post, stage_to_fetch)
        for post in posts:
            metrics: dict = post.get("metrics", {})

            # Skip posts marked as unfetchable (deleted, permissions error, etc.)
            if metrics.get("fetch_status") == "unfetchable":
                continue

            try:
                posted_at = datetime.datetime.fromisoformat(post["posted_at"])
            except (KeyError, ValueError, TypeError):
                self.logger.warning(
                    "Invalid posted_at for %s — skipping.", post.get("id", "?"),
                )
                continue
            age_hours = (now - posted_at).total_seconds() / 3600

            current_stage = metrics.get("current_stage")

            if current_stage is None:
                # Never measured — check if old enough for first stage
                if age_hours >= min_age:
                    eligible.append((post, first_stage))
            elif current_stage == "1h":
                # Legacy: migrate posts stuck on old 1h stage → treat as needing 6h
                if age_hours >= _STAGE_MIN_AGE["6h"]:
                    eligible.append((post, "6h"))
            else:
                next_stage = _STAGE_NEXT.get(current_stage)
                if next_stage is None:
                    continue  # all stages complete
                if age_hours >= _STAGE_MIN_AGE[next_stage]:
                    eligible.append((post, next_stage))

        if not eligible:
            self.logger.info("No posts eligible for metric fetching.")
            return

        self.logger.info("Fetching insights for %d post(s).", len(eligible))

        # 2. Fetch and update metrics for each eligible post
        fetched_at = now.isoformat()
        fetched_records: list[dict] = []

        for post, stage in eligible:
            media_id: str = post.get("threads_media_id", "")
            if not media_id:
                self.logger.warning(
                    "Post %s has no threads_media_id — skipping.", post.get("id")
                )
                continue

            post_metrics = post.setdefault("metrics", {})

            try:
                insights = self.threads.get_post_insights(media_id)
            except (AuthenticationError, RateLimitError):
                # Let BaseAgent handle auth failures (emergency stop) and
                # rate limits (backoff).  Save any pending state first.
                history_data["last_updated"] = fetched_at
                self.state.save_json("post_history.json", history_data)
                raise
            except ThreadsAPIError as exc:
                error_msg = str(exc)
                post_id = post.get("id", "?")

                # Track consecutive failures
                fail_count = post_metrics.get("fetch_fail_count", 0) + 1
                post_metrics["fetch_fail_count"] = fail_count

                # Detect deleted / permanently inaccessible posts
                is_permanent = ("does not exist" in error_msg
                                or "HTTP 404" in error_msg
                                or "cannot be loaded" in error_msg)

                if is_permanent or fail_count >= _MAX_FETCH_FAILURES:
                    post_metrics["fetch_status"] = "unfetchable"
                    reason = "deleted_or_inaccessible" if is_permanent else "max_failures"
                    post_metrics["fetch_error_reason"] = reason
                    post_metrics["fetch_error_at"] = now.isoformat()
                    self.logger.warning(
                        "Marked %s as unfetchable (reason=%s, failures=%d).",
                        post_id, reason, fail_count,
                    )
                else:
                    self.logger.error(
                        "Failed to fetch insights for %s (media_id=%s, attempt %d): %s",
                        post_id, media_id, fail_count, exc,
                    )
                continue
            except Exception as exc:
                self.logger.error(
                    "Unexpected error fetching %s (media_id=%s): %s",
                    post.get("id", "?"), media_id, exc,
                )
                continue

            # 3. Update metrics in post_history (time-series per stage)
            stages = post_metrics.setdefault("stages", {})

            # Clear failure counter on success
            post_metrics.pop("fetch_fail_count", None)

            stage_snapshot = {
                "views": insights.get("views", 0),
                "likes": insights.get("likes", 0),
                "replies": insights.get("replies", 0),
                "reposts": insights.get("reposts", 0),
                "quotes": insights.get("quotes", 0),
                "fetched_at": fetched_at,
            }
            stages[stage] = stage_snapshot
            post_metrics["current_stage"] = stage
            post_metrics["last_fetched"] = fetched_at

            # Also keep flat "current" metrics for backward compat
            for k in ("views", "likes", "replies", "reposts", "quotes"):
                post_metrics[k] = stage_snapshot[k]

            # Calculate engagement_rate on every stage (not just 24h)
            total_eng = (
                stage_snapshot["likes"] + stage_snapshot["replies"]
                + stage_snapshot["reposts"] + stage_snapshot["quotes"]
            )
            post_metrics["engagement_rate"] = round(
                (total_eng / stage_snapshot["views"] * 100)
                if stage_snapshot["views"] else 0.0,
                2,
            )

            fetched_records.append(
                _build_performance_record(post, insights, stage, fetched_at)
            )

            self.logger.info(
                "Updated metrics for %s [stage=%s]: views=%d, likes=%d",
                post.get("id"),
                stage,
                stage_snapshot["views"],
                stage_snapshot["likes"],
            )

        # Save updated post_history
        history_data["last_updated"] = fetched_at
        self.state.save_json("post_history.json", history_data)

        # 4. Update performance.json
        if fetched_records:
            self._update_performance(fetched_records, fetched_at)

        # 5. Retroactive affiliate injection for buzz posts
        if self.affiliate_mode == "retroactive" and fetched_records:
            self._inject_affiliate_for_buzz_posts(history_data)

        # 6. Collect buzz post hooks for hook_stock.json
        if fetched_records:
            self._collect_buzz_hooks(history_data)

        self.logger.info("Fetcher run complete — %d post(s) updated.", len(fetched_records))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _inject_affiliate_for_buzz_posts(
        self, history_data: dict[str, Any]
    ) -> None:
        """Check recent posts for buzz thresholds and add affiliate comments.

        A post qualifies for affiliate injection when:
        1. views >= buzz_views_threshold
        2. engagement_rate >= buzz_engagement_threshold
        3. No affiliate comment has been added yet (affiliate_added_at is None)
        4. Daily affiliate budget is not exhausted
        """
        now = datetime.datetime.now(JST)
        today_key = now.strftime("%Y-%m-%d")
        posts: list[dict[str, Any]] = history_data.get("posts", [])

        # Count how many affiliate comments we've already added today
        today_affiliate_count = sum(
            1 for p in posts
            if (p.get("affiliate_added_at") or "").startswith(today_key)
        )
        remaining_budget = self.max_affiliate_per_day - today_affiliate_count

        if remaining_budget <= 0:
            self.logger.info("Daily affiliate budget exhausted (%d/%d).",
                             today_affiliate_count, self.max_affiliate_per_day)
            return

        injected = 0
        for post in posts:
            if injected >= remaining_budget:
                break

            # Skip if already has affiliate
            if post.get("affiliate_added_at"):
                continue

            # Check metrics
            metrics = post.get("metrics", {})
            views = metrics.get("views", 0)
            if views < self.buzz_views:
                continue

            total_engagement = (
                metrics.get("likes", 0)
                + metrics.get("replies", 0)
                + metrics.get("reposts", 0)
                + metrics.get("quotes", 0)
            )
            eng_rate = total_engagement / views if views else 0.0
            if eng_rate < self.buzz_engagement:
                continue

            # Post qualifies as buzz — generate affiliate comment
            media_id = post.get("threads_media_id", "")
            if not media_id:
                continue

            self.logger.info(
                "Buzz detected for %s (views=%d, eng=%.2f%%). Adding affiliate.",
                post.get("id", "?"), views, eng_rate * 100,
            )

            try:
                affiliate_text = self._generate_affiliate_comment(post)
                if not affiliate_text:
                    continue

                # Post as self-reply
                result = self.threads.create_text_post(
                    affiliate_text,
                    reply_to_id=media_id,
                )

                # Record in history
                post["affiliate_added_at"] = now.isoformat()
                post["affiliate_media_id"] = result.get("id", "")
                injected += 1

                # Notify after successful injection (use stable post ID for dedup)
                post_id = post.get("id", "?")
                self.notifier.send(
                    "buzz_detected",
                    f"Buzz+Affiliate: {post_id} views={views} eng={eng_rate*100:.1f}%",
                    SEVERITY_MEDIUM,
                )

                self.logger.info(
                    "Affiliate comment added to %s (media_id=%s).",
                    post.get("id", "?"),
                    result.get("id", ""),
                )

            except Exception as exc:
                self.logger.error(
                    "Failed to add affiliate to %s: %s",
                    post.get("id", "?"), exc,
                )

        if injected:
            history_data["last_updated"] = now.isoformat()
            self.state.save_json("post_history.json", history_data)
            self.logger.info("Retroactive affiliate: %d comment(s) added.", injected)

    def _generate_affiliate_comment(self, post: dict[str, Any]) -> str | None:
        """Generate a PR-labelled affiliate comment for a buzz post.

        Uses the product catalog to select the best-matching product and
        generates a natural recommendation pointing to the profile link.

        Args:
            post: The post dict from post_history.json.

        Returns:
            The affiliate comment text with PR label, or None on failure.
        """
        category = post.get("category", "skincare_knowledge")
        content = post.get("content", "")

        # Select best-matching product from catalog
        product = self._select_product(category, content)
        display_hint = product.get("display_hint", "") if product else ""

        if product:
            prompt = (
                "## 元投稿\n"
                f"{content}\n\n"
                f"## カテゴリ\n{category}\n\n"
                f"## おすすめ商品のヒント\n{display_hint}\n\n"
                "## 指示\n"
                "上記の投稿にアフィリエイトのPRコメントを追加してください。\n"
                "- 1行目は必ず「PR」と記載\n"
                "- おすすめ商品のヒントを元に、元投稿の内容に関連づけて自然におすすめ\n"
                "- 製品名・ブランド名は絶対に出さない\n"
                "- 「プロフのリンク見てみて」でプロフィールへ誘導\n"
                "- 30〜120文字\n"
                "- 押し売り感を出さない\n"
                "- PRコメントのテキストのみを出力\n"
            )
        else:
            prompt = (
                "## 元投稿\n"
                f"{content}\n\n"
                f"## カテゴリ\n{category}\n\n"
                "## 指示\n"
                "上記の投稿にアフィリエイトのPRコメントを追加してください。\n"
                "- 1行目は必ず「PR」と記載\n"
                "- 元投稿の内容に関連する商品をおすすめする自然な一言\n"
                "- 「プロフのリンク見てみて」でプロフィールへ誘導\n"
                "- 30〜120文字\n"
                "- 押し売り感を出さない\n"
                "- PRコメントのテキストのみを出力\n"
            )

        try:
            text = self.claude_client.generate_post(prompt=prompt)
            text = text.strip()

            # Ensure PR label
            if not text.upper().startswith("PR"):
                text = f"PR\n{text}"

            if len(text) < 10 or len(text) > 200:
                return None

            # Record which product was used for weekly limit tracking
            if product:
                self._record_product_usage(post, product["id"])

            return text

        except Exception as exc:
            self.logger.error("Affiliate comment generation failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Product catalog helpers
    # ------------------------------------------------------------------

    def _load_product_catalog(self) -> list[dict[str, Any]]:
        """Load and cache the product catalog from affiliate_products.yaml."""
        if self._product_catalog is not None:
            return self._product_catalog

        catalog_path = _PROJECT_ROOT / "config" / "affiliate_products.yaml"
        try:
            with open(catalog_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.logger.warning("Product catalog not found: %s", catalog_path)
            self._product_catalog = []
            self._matching_cfg = {}
            return []

        self._product_catalog = [
            p for p in data.get("products", []) if p.get("active", True)
        ]
        self._matching_cfg = data.get("matching", {})
        return self._product_catalog

    def _select_product(
        self, category: str, content: str
    ) -> dict[str, Any] | None:
        """Select the best-matching product for a post.

        Matching strategy:
        1. Filter by category
        2. Score by keyword overlap with post content
        3. Prefer high-tier products
        4. Respect weekly usage limits

        Returns:
            The best product dict, or None if no match.
        """
        products = self._load_product_catalog()
        if not products:
            return None

        min_overlap = self._matching_cfg.get("min_keyword_overlap", 1)
        max_per_week = self._matching_cfg.get("max_same_product_per_week", 3)
        content_lower = content.lower()

        scored: list[tuple[int, int, dict[str, Any]]] = []
        for prod in products:
            # Category match (soft: also allow if no category filter)
            if prod.get("category") and prod["category"] != category:
                continue

            # Keyword overlap scoring
            keywords = prod.get("keywords", [])
            overlap = sum(1 for kw in keywords if kw in content or kw in content_lower)
            if overlap < min_overlap:
                continue

            # Weekly limit check
            if self._get_weekly_product_count(prod["id"]) >= max_per_week:
                continue

            # Tier bonus: high=3, medium=2, low=1
            tier_bonus = {"high": 3, "medium": 2, "low": 1}.get(
                prod.get("tier", "low"), 1
            )
            priority = prod.get("priority", 0)

            scored.append((overlap + tier_bonus, priority, prod))

        if not scored:
            return None

        # Sort by (overlap+tier desc, priority desc)
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return scored[0][2]

    def _get_weekly_product_count(self, product_id: str) -> int:
        """Count how many times a product was recommended in the past 7 days."""
        history_data = self.state.load_json("post_history.json")
        posts = history_data.get("posts", [])
        now = datetime.datetime.now(JST)
        week_ago = now - datetime.timedelta(days=7)

        count = 0
        for post in posts:
            if post.get("affiliate_product_id") != product_id:
                continue
            added_at = post.get("affiliate_added_at")
            if not added_at:
                continue
            try:
                dt = datetime.datetime.fromisoformat(added_at)
                if dt >= week_ago:
                    count += 1
            except (ValueError, TypeError):
                pass
        return count

    def _record_product_usage(self, post: dict[str, Any], product_id: str) -> None:
        """Record which product was used for a post's affiliate comment."""
        post["affiliate_product_id"] = product_id

    def _collect_buzz_hooks(self, history_data: dict[str, Any]) -> None:
        """Extract first lines from buzz posts and save to hook_stock.json.

        A post qualifies if views >= buzz threshold and engagement_rate >=
        buzz engagement threshold, and its hook hasn't been collected yet.
        """
        import json as _json

        hook_path = _PROJECT_ROOT / "knowledge" / "hook_stock.json"

        try:
            with open(hook_path, "r", encoding="utf-8") as f:
                hook_data = _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError):
            hook_data = {"last_updated": None, "hooks": []}

        existing_post_ids: set[str] = {
            h.get("source_post_id", "") for h in hook_data.get("hooks", [])
        }

        now = datetime.datetime.now(JST)
        posts = history_data.get("posts", [])
        added = 0

        for post in posts:
            post_id = post.get("id", "")
            if post_id in existing_post_ids:
                continue

            metrics = post.get("metrics", {})
            views = metrics.get("views", 0)
            if views < self.buzz_views:
                continue

            total_eng = (
                metrics.get("likes", 0)
                + metrics.get("replies", 0)
                + metrics.get("reposts", 0)
                + metrics.get("quotes", 0)
            )
            eng_rate = total_eng / views if views else 0.0
            if eng_rate < self.buzz_engagement:
                continue

            content = post.get("content", "")
            if not content:
                continue

            first_line = content.split("\n")[0].strip()
            if len(first_line) < 5 or len(first_line) > 60:
                continue

            hook_data["hooks"].append({
                "id": f"hook_{now.strftime('%Y%m%d')}_{added + 1:03d}",
                "text": first_line,
                "source_post_id": post_id,
                "pattern": post.get("pattern", ""),
                "category": post.get("category", ""),
                "views": views,
                "engagement_rate": round(eng_rate, 4),
                "collected_at": now.isoformat(),
            })
            existing_post_ids.add(post_id)
            added += 1

        if added:
            # Cap at 200 hooks (keep the best by engagement_rate)
            hooks = hook_data["hooks"]
            hooks.sort(key=lambda h: h.get("engagement_rate", 0), reverse=True)
            hook_data["hooks"] = hooks[:200]
            hook_data["last_updated"] = now.isoformat()

            import os
            import tempfile

            fd, tmp_path = tempfile.mkstemp(
                dir=str(hook_path.parent), suffix=".tmp", prefix="hook_stock"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _json.dump(hook_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(hook_path))
            except OSError:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            self.logger.info(
                "Collected %d buzz hooks (total stock: %d).",
                added, len(hook_data["hooks"]),
            )

    def _update_performance(
        self, new_records: list[dict], fetched_at: str
    ) -> None:
        """Merge *new_records* into ``data/analytics/performance.json``.

        Existing entries for the same ``post_id`` are replaced; new entries are
        appended.
        """
        _ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
        perf_path = _ANALYTICS_DIR / "performance.json"

        # Load existing performance data
        import json

        perf_data: dict[str, Any]
        if perf_path.exists():
            try:
                with open(perf_path, "r", encoding="utf-8") as f:
                    perf_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                perf_data = {"last_updated": None, "records": []}
        else:
            perf_data = {"last_updated": None, "records": []}

        # Migrate legacy "posts" key to "records" (V1 → V2)
        if "posts" in perf_data and "records" not in perf_data:
            perf_data["records"] = perf_data.pop("posts")

        existing_posts: list[dict] = perf_data.setdefault("records", [])

        # Build a lookup by post_id for fast upsert
        index: dict[str, int] = {
            p["post_id"]: idx for idx, p in enumerate(existing_posts)
        }

        for record in new_records:
            post_id = record["post_id"]
            if post_id in index:
                existing_posts[index[post_id]] = record
            else:
                existing_posts.append(record)
                index[post_id] = len(existing_posts) - 1

        perf_data["last_updated"] = fetched_at

        # Write atomically via a temp file
        import os
        import tempfile

        fd, tmp_path = tempfile.mkstemp(
            dir=str(_ANALYTICS_DIR), suffix=".tmp", prefix="performance.json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(perf_data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_path, str(perf_path))
        except OSError:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


def _build_performance_record(
    post: dict, insights: dict, stage: str, fetched_at: str
) -> dict:
    """Create a single record suitable for ``performance.json``."""
    total_views = insights.get("views", 0)
    total_engagement = (
        insights.get("likes", 0)
        + insights.get("replies", 0)
        + insights.get("reposts", 0)
        + insights.get("quotes", 0)
    )
    engagement_rate = (total_engagement / total_views * 100) if total_views else 0.0

    content = post.get("content", "")
    return {
        "post_id": post.get("id", ""),
        "threads_media_id": post.get("threads_media_id", ""),
        "posted_at": post.get("posted_at", ""),
        "category": post.get("category", ""),
        "pattern": post.get("pattern", ""),
        "quality_score": post.get("quality_score", 0.0),
        "content_length": len(content),
        "stage": stage,
        "views": insights.get("views", 0),
        "likes": insights.get("likes", 0),
        "replies": insights.get("replies", 0),
        "reposts": insights.get("reposts", 0),
        "quotes": insights.get("quotes", 0),
        "engagement_rate": round(engagement_rate, 2),
        "fetched_at": fetched_at,
    }
