"""Analyst agent: analyses post performance and generates feedback."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from agents.base_agent import BaseAgent
from core.constants import JST
from core.logger import get_logger
from core.notifier import Notifier
from services.claude_client import ClaudeClient

logger = get_logger("analyst")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "config" / "schedule.yaml"
_PERFORMANCE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "analytics" / "performance.json"
)
_AUDIENCE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "analytics" / "audience.json"
)
_THEME_TREE_PATH = (
    Path(__file__).resolve().parent.parent / "knowledge" / "theme_tree.yaml"
)


class AnalystAgent(BaseAgent):
    """Analyse post performance data and generate actionable feedback.

    The analyst reads ``data/analytics/performance.json``, computes
    engagement statistics broken down by category, posting pattern, and
    hour-of-day, then asks Claude to produce qualitative feedback.
    Results are written back to ``performance.json`` (summary) and
    ``data/analytics/audience.json`` (feedback).  Schedule
    recommendations are propagated to ``config/schedule.yaml``.
    """

    def __init__(self) -> None:
        super().__init__("analyst")
        self.claude = ClaudeClient()
        self.config = self._load_analyst_config()
        self.notifier = Notifier()

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    def execute(self) -> None:
        """Run the full analysis pipeline.

        Steps:
            1. Load performance data.
            2. Skip if fewer than ``min_posts_for_analysis`` posts.
            3. Compute quantitative analysis.
            4. Generate qualitative feedback via Claude.
            5. Update performance.json summary.
            6. Update schedule.yaml posting-pattern recommendations.
        """
        perf = self._load_performance()
        posts: list[dict[str, Any]] = perf.get("records", [])

        min_posts: int = self.config.get("min_posts_for_analysis", 10)
        if len(posts) < min_posts:
            self.logger.info(
                "Not enough posts for analysis (%d < %d). Skipping.",
                len(posts),
                min_posts,
            )
            return

        # 3. Quantitative analysis
        analysis = self._analyze_performance(posts)
        self.logger.info("Analysis complete: trend=%s", analysis.get("trend"))

        # 4. Qualitative feedback via Claude
        feedback = self._generate_feedback(analysis, posts)
        self.logger.info(
            "Feedback generated: top_categories=%s", feedback.get("top_categories")
        )

        # 5. Update performance.json summary
        self._update_performance_summary(perf, analysis)

        # 6. Update schedule.yaml posting-pattern recommendations
        self._update_schedule_recommendations(analysis, feedback)

        # 7. Review trend analysis (draft_queue reject/edit patterns)
        review_trends = self._analyze_review_trends()
        if review_trends:
            feedback["review_trends"] = review_trends

        # 8. Save audience feedback (single write at end)
        self._save_audience_feedback(feedback, analysis)

        # 9. Update theme tree coverage metadata
        self._update_theme_tree(analysis)

        # 10. Rotate performance.json (drop records older than 90 days)
        self._rotate_performance_records(perf)

        # 11. Archive old posts from post_history.json
        archived = self.state.archive_old_posts()
        if archived:
            self.logger.info("Archived %d old posts from post_history.", archived)

        # 12. Send daily report via Telegram
        self._send_daily_report(analysis, posts)

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def _analyze_performance(self, posts: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute quantitative performance metrics.

        Args:
            posts: List of post records from ``performance.json``.

        Returns:
            A dict with keys ``by_category``, ``by_pattern``, ``by_hour``,
            and ``trend``.
        """
        now = datetime.now(JST)

        # --- by_category ---
        cat_stats: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: {"views": [], "likes": [], "replies": [], "engagement_rate": []}
        )
        for p in posts:
            cat = p.get("category", "unknown")
            cat_stats[cat]["views"].append(p.get("views", 0))
            cat_stats[cat]["likes"].append(p.get("likes", 0))
            cat_stats[cat]["replies"].append(p.get("replies", 0))
            cat_stats[cat]["engagement_rate"].append(p.get("engagement_rate", 0.0))

        by_category: dict[str, dict[str, float]] = {}
        for cat, metrics in cat_stats.items():
            by_category[cat] = {
                "avg_views": _safe_avg(metrics["views"]),
                "avg_likes": _safe_avg(metrics["likes"]),
                "avg_replies": _safe_avg(metrics["replies"]),
                "avg_engagement_rate": _safe_avg(metrics["engagement_rate"]),
                "post_count": len(metrics["views"]),
            }

        # --- by_pattern ---
        pat_stats: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: {"views": [], "likes": [], "replies": [], "engagement_rate": []}
        )
        for p in posts:
            pattern = p.get("pattern", "unknown")
            pat_stats[pattern]["views"].append(p.get("views", 0))
            pat_stats[pattern]["likes"].append(p.get("likes", 0))
            pat_stats[pattern]["replies"].append(p.get("replies", 0))
            pat_stats[pattern]["engagement_rate"].append(
                p.get("engagement_rate", 0.0)
            )

        by_pattern: dict[str, dict[str, float]] = {}
        for pat, metrics in pat_stats.items():
            by_pattern[pat] = {
                "avg_views": _safe_avg(metrics["views"]),
                "avg_likes": _safe_avg(metrics["likes"]),
                "avg_replies": _safe_avg(metrics["replies"]),
                "avg_engagement_rate": _safe_avg(metrics["engagement_rate"]),
                "post_count": len(metrics["views"]),
            }

        # --- by_hour ---
        hour_stats: dict[int, dict[str, list[float]]] = defaultdict(
            lambda: {"views": [], "likes": [], "replies": [], "engagement_rate": []}
        )
        for p in posts:
            posted_at = p.get("posted_at")
            if posted_at:
                try:
                    dt = datetime.fromisoformat(posted_at)
                    hour = dt.hour
                except (ValueError, TypeError):
                    continue
                hour_stats[hour]["views"].append(p.get("views", 0))
                hour_stats[hour]["likes"].append(p.get("likes", 0))
                hour_stats[hour]["replies"].append(p.get("replies", 0))
                hour_stats[hour]["engagement_rate"].append(
                    p.get("engagement_rate", 0.0)
                )

        by_hour: dict[str, dict[str, float]] = {}
        for h, metrics in sorted(hour_stats.items()):
            by_hour[str(h)] = {
                "avg_views": _safe_avg(metrics["views"]),
                "avg_likes": _safe_avg(metrics["likes"]),
                "avg_replies": _safe_avg(metrics["replies"]),
                "avg_engagement_rate": _safe_avg(metrics["engagement_rate"]),
                "post_count": len(metrics["views"]),
            }

        # --- trend: last 7 days vs previous 7 days ---
        seven_days_ago = now - timedelta(days=7)
        fourteen_days_ago = now - timedelta(days=14)

        recent: list[dict[str, Any]] = []
        previous: list[dict[str, Any]] = []
        for p in posts:
            posted_at = p.get("posted_at")
            if not posted_at:
                continue
            try:
                dt = datetime.fromisoformat(posted_at)
            except (ValueError, TypeError):
                continue
            if dt >= seven_days_ago:
                recent.append(p)
            elif dt >= fourteen_days_ago:
                previous.append(p)

        recent_avg_eng = _safe_avg(
            [p.get("engagement_rate", 0.0) for p in recent]
        )
        prev_avg_eng = _safe_avg(
            [p.get("engagement_rate", 0.0) for p in previous]
        )

        if prev_avg_eng > 0 and recent_avg_eng > 0:
            change_pct = ((recent_avg_eng - prev_avg_eng) / prev_avg_eng) * 100
            if change_pct > 10:
                trend = f"improving (+{change_pct:.1f}%)"
            elif change_pct < -10:
                trend = f"declining ({change_pct:.1f}%)"
            else:
                trend = f"stable ({change_pct:+.1f}%)"
        elif not previous:
            trend = "insufficient_previous_data"
        else:
            trend = "stable"

        # --- TOP5 + BOTTOM3 (stratified by category, last 30 days) ---
        thirty_days_ago = now - timedelta(days=30)
        recent_30d = []
        for p in posts:
            pa = p.get("posted_at")
            if not pa:
                continue
            try:
                dt = datetime.fromisoformat(pa)
            except (ValueError, TypeError):
                continue
            if dt >= thirty_days_ago and p.get("engagement_rate", 0) > 0:
                recent_30d.append(p)

        # TOP5: highest engagement, max 2 per category
        sorted_top = sorted(
            recent_30d, key=lambda x: x.get("engagement_rate", 0), reverse=True
        )
        top5: list[dict[str, Any]] = []
        top_cat_counts: dict[str, int] = defaultdict(int)
        for p in sorted_top:
            cat = p.get("category", "unknown")
            if top_cat_counts[cat] >= 2:
                continue
            top5.append({
                "post_id": p.get("post_id", ""),
                "engagement_rate": p.get("engagement_rate", 0),
                "category": cat,
                "pattern": p.get("pattern", ""),
                "content_preview": p.get("content", "")[:80],
            })
            top_cat_counts[cat] += 1
            if len(top5) >= 5:
                break

        # BOTTOM3: lowest engagement
        sorted_bottom = sorted(
            recent_30d, key=lambda x: x.get("engagement_rate", 0)
        )
        bottom3 = [
            {
                "post_id": p.get("post_id", ""),
                "engagement_rate": p.get("engagement_rate", 0),
                "category": p.get("category", ""),
                "pattern": p.get("pattern", ""),
                "content_preview": p.get("content", "")[:80],
            }
            for p in sorted_bottom[:3]
        ]

        # --- Category entropy (diversity measure) ---
        import math
        cat_counts = [
            data["post_count"]
            for data in by_category.values()
            if data.get("post_count", 0) > 0
        ]
        total_cat_posts = sum(cat_counts)
        if total_cat_posts > 0 and len(cat_counts) > 1:
            probs = [c / total_cat_posts for c in cat_counts]
            entropy = -sum(p * math.log(p) for p in probs if p > 0)
            max_entropy = math.log(len(cat_counts))
            normalized_entropy = round(entropy / max_entropy, 2) if max_entropy > 0 else 0
        else:
            entropy = 0.0
            normalized_entropy = 0.0

        return {
            "by_category": by_category,
            "by_pattern": by_pattern,
            "by_hour": by_hour,
            "trend": trend,
            "recent_avg_engagement": recent_avg_eng,
            "previous_avg_engagement": prev_avg_eng,
            "top5": top5,
            "bottom3": bottom3,
            "category_entropy": round(entropy, 2),
            "category_entropy_normalized": normalized_entropy,
            "entropy_warning": normalized_entropy < 0.6,
        }

    # ------------------------------------------------------------------
    # Feedback generation
    # ------------------------------------------------------------------

    def _generate_feedback(
        self, analysis: dict[str, Any], posts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Ask Claude to produce qualitative feedback from the analysis.

        Args:
            analysis: Output of :meth:`_analyze_performance`.
            posts: Raw post records.

        Returns:
            A dict with keys ``top_categories``, ``weak_categories``,
            ``recommended_patterns``, ``avoid_patterns``, and ``insights``.
        """
        prompt = (
            "あなたはSNS運用のデータアナリストです。\n"
            "以下の投稿パフォーマンス分析結果をもとに、次バッチの投稿戦略を提案してください。\n\n"
            "回答は必ず以下のJSON形式で返してください（JSON以外のテキストは不要）:\n"
            "{\n"
            '  "top_categories": ["カテゴリ名", ...],\n'
            '  "weak_categories": ["カテゴリ名", ...],\n'
            '  "recommended_patterns": ["パターン名", ...],\n'
            '  "avoid_patterns": ["パターン名", ...],\n'
            '  "insights": ["発見事項1", "発見事項2", ...]\n'
            "}\n\n"
            "分析観点:\n"
            "- 伸びた投稿の共通点\n"
            "- 改善すべき点\n"
            "- 次バッチへの推奨（カテゴリ・パターン・時間帯）"
        )

        data_str = json.dumps(analysis, ensure_ascii=False, indent=2)

        raw_response = self.claude.analyze(prompt=prompt, data=data_str)

        # Parse JSON from response
        try:
            feedback: dict[str, Any] = json.loads(raw_response)
        except json.JSONDecodeError:
            # Attempt to extract JSON from markdown fences or surrounding text
            start = raw_response.find("{")
            end = raw_response.rfind("}") + 1
            if start != -1 and end > start:
                feedback = json.loads(raw_response[start:end])
            else:
                self.logger.warning(
                    "Could not parse feedback JSON from Claude response."
                )
                feedback = {
                    "top_categories": [],
                    "weak_categories": [],
                    "recommended_patterns": [],
                    "avoid_patterns": [],
                    "insights": [raw_response[:500]],
                }

        # Ensure expected keys
        feedback.setdefault("top_categories", [])
        feedback.setdefault("weak_categories", [])
        feedback.setdefault("recommended_patterns", [])
        feedback.setdefault("avoid_patterns", [])
        feedback.setdefault("insights", [])

        return feedback

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_performance(self) -> dict[str, Any]:
        """Load ``data/analytics/performance.json``."""
        if _PERFORMANCE_PATH.exists():
            with open(_PERFORMANCE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migrate legacy "posts" key to "records" (V1 → V2)
            if "posts" in data and "records" not in data:
                data["records"] = data.pop("posts")
            return data
        return {"records": [], "summary": {}, "by_category": {}}

    def _update_performance_summary(
        self, perf: dict[str, Any], analysis: dict[str, Any]
    ) -> None:
        """Write the analysis summary back into performance.json.

        Args:
            perf: The full performance data dict.
            analysis: Output of :meth:`_analyze_performance`.
        """
        now = datetime.now(JST)

        # Determine best posting hours (top 4 by avg engagement rate)
        by_hour = analysis.get("by_hour", {})
        sorted_hours = sorted(
            by_hour.items(),
            key=lambda kv: kv[1].get("avg_engagement_rate", 0),
            reverse=True,
        )
        best_hours = [int(h) for h, _ in sorted_hours[:4]]

        # Determine best categories
        by_category = analysis.get("by_category", {})
        sorted_cats = sorted(
            by_category.items(),
            key=lambda kv: kv[1].get("avg_engagement_rate", 0),
            reverse=True,
        )
        top_category = sorted_cats[0][0] if sorted_cats else "unknown"

        # Compute global averages
        all_views = [
            v
            for cat_data in by_category.values()
            for v in [cat_data.get("avg_views", 0)]
        ]
        all_likes = [
            v
            for cat_data in by_category.values()
            for v in [cat_data.get("avg_likes", 0)]
        ]
        all_replies = [
            v
            for cat_data in by_category.values()
            for v in [cat_data.get("avg_replies", 0)]
        ]

        perf["summary"] = {
            "period": "last_30_days",
            "avg_views": round(_safe_avg(all_views), 1),
            "avg_likes": round(_safe_avg(all_likes), 1),
            "avg_replies": round(_safe_avg(all_replies), 1),
            "top_category": top_category,
            "best_posting_hours": best_hours,
            "trend": analysis.get("trend", "unknown"),
            "analyzed_at": now.isoformat(),
        }

        perf["by_category"] = analysis.get("by_category", {})
        perf["last_updated"] = now.isoformat()

        _PERFORMANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_PERFORMANCE_PATH, "w", encoding="utf-8") as f:
            json.dump(perf, f, ensure_ascii=False, indent=2, default=str)

        self.logger.info("Updated performance.json summary.")

    def _send_daily_report(
        self, analysis: dict[str, Any], posts: list[dict[str, Any]]
    ) -> None:
        """Send daily KPI summary via Telegram notification."""
        from services.token_manager import TokenManager

        # Build report data
        by_category = analysis.get("by_category", {})
        sorted_cats = sorted(
            by_category.items(),
            key=lambda kv: kv[1].get("avg_engagement_rate", 0),
            reverse=True,
        )
        top_category = sorted_cats[0][0] if sorted_cats else "unknown"
        top_er = sorted_cats[0][1].get("avg_engagement_rate", 0) if sorted_cats else 0

        # Count today's posts
        today = datetime.now(JST).strftime("%Y-%m-%d")
        today_posts = sum(
            1 for p in posts
            if (p.get("posted_at") or "").startswith(today)
        )

        # Global avg engagement
        all_er = [
            cat_data.get("avg_engagement_rate", 0)
            for cat_data in by_category.values()
        ]
        avg_eng = sum(all_er) / len(all_er) if all_er else 0

        # Token status
        try:
            tm = TokenManager(self.state)
            token_status = tm.get_token_status()
            token_days = token_status.get("days_remaining", "?")
        except Exception:
            token_days = "?"

        # Queue sizes
        draft_queue = self.state.load_json("draft_queue.json")
        post_queue = self.state.load_json("post_queue.json")
        research_pool = self.state.load_json("research_pool.json")

        draft_count = len(draft_queue.get("drafts", []))
        post_count = len([
            q for q in post_queue.get("queue", [])
            if q.get("status") == "pending"
        ])
        research_count = len([
            r for r in research_pool.get("items", [])
            if not r.get("used")
        ])

        # Compute average quality score from post records
        quality_scores = [
            p.get("quality_score", 0)
            for p in posts
            if isinstance(p.get("quality_score"), (int, float))
        ]
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0

        report = {
            "posts_today": today_posts,
            "avg_engagement": avg_eng,
            "avg_quality_score": avg_quality,
            "top_post": f"{top_category} (ER {top_er:.1f}%)",
        }

        self.notifier.send_daily_report(report)

        # Also send text summary with queue info
        self.notifier.send(
            "daily_report",
            (
                f"[Daily Report] {today}\n"
                f"投稿: {today_posts}件 | Avg ER: {avg_eng:.1f}%\n"
                f"Top: {top_category} (ER {top_er:.1f}%)\n"
                f"Queue: draft {draft_count} / post {post_count} / research {research_count}\n"
                f"Token: {token_days}日"
            ),
        )

    def _rotate_performance_records(self, perf: dict[str, Any]) -> None:
        """Drop performance records older than 90 days."""
        now = datetime.now(JST)
        cutoff = now - timedelta(days=90)
        records: list[dict[str, Any]] = perf.get("records", [])
        original_count = len(records)

        kept = []
        for rec in records:
            posted_at_str = rec.get("posted_at")
            if not posted_at_str:
                kept.append(rec)
                continue
            try:
                posted_at = datetime.fromisoformat(posted_at_str)
            except (ValueError, TypeError):
                kept.append(rec)
                continue
            if posted_at >= cutoff:
                kept.append(rec)

        removed = original_count - len(kept)
        if removed > 0:
            perf["records"] = kept
            perf["last_updated"] = now.isoformat()
            _PERFORMANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_PERFORMANCE_PATH, "w", encoding="utf-8") as f:
                json.dump(perf, f, ensure_ascii=False, indent=2, default=str)
            self.logger.info(
                "Rotated performance.json: removed %d old records, %d kept.",
                removed, len(kept),
            )

    def _save_audience_feedback(
        self, feedback: dict[str, Any], analysis: dict[str, Any]
    ) -> None:
        """Persist feedback to ``data/analytics/audience.json``.

        Args:
            feedback: Claude-generated feedback dict.
            analysis: Quantitative analysis dict.
        """
        now = datetime.now(JST)
        audience_data: dict[str, Any] = {
            "last_updated": now.isoformat(),
            "feedback": feedback,
            "top5": analysis.get("top5", []),
            "bottom3": analysis.get("bottom3", []),
            "analysis_snapshot": {
                "trend": analysis.get("trend"),
                "recent_avg_engagement": analysis.get("recent_avg_engagement"),
                "previous_avg_engagement": analysis.get("previous_avg_engagement"),
            },
        }

        _AUDIENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIENCE_PATH, "w", encoding="utf-8") as f:
            json.dump(audience_data, f, ensure_ascii=False, indent=2, default=str)

        self.logger.info("Saved audience feedback to audience.json.")

    def _analyze_review_trends(self) -> dict[str, Any] | None:
        """Analyze draft_queue review patterns (reject reasons, edit frequency).

        Returns:
            A dict with review trend data, or None if no review data.
        """
        draft_data = self.state.load_json("draft_queue.json")
        drafts = draft_data.get("drafts", [])

        reviewed = [d for d in drafts if d.get("review")]
        if not reviewed:
            return None

        # Count reject reasons
        reject_reasons: dict[str, int] = defaultdict(int)
        reject_by_pattern: dict[str, int] = defaultdict(int)
        reject_by_category: dict[str, int] = defaultdict(int)
        edit_count = 0
        total_reviewed = 0

        for d in reviewed:
            review = d["review"]
            action = review.get("action", "")
            total_reviewed += 1

            if action == "rejected":
                reason = review.get("reason_code", "unknown")
                reject_reasons[reason] += 1
                reject_by_pattern[d.get("pattern", "unknown")] += 1
                reject_by_category[d.get("category", "unknown")] += 1
            elif action == "edited":
                edit_count += 1

        stats = draft_data.get("stats", {})
        total_generated = stats.get("total_generated", 0)
        total_rejected = stats.get("rejected", 0)

        result: dict[str, Any] = {
            "total_reviewed": total_reviewed,
            "reject_rate": round(total_rejected / total_generated * 100, 1) if total_generated else 0,
            "edit_rate": round(edit_count / total_reviewed * 100, 1) if total_reviewed else 0,
            "top_reject_reasons": dict(
                sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)[:5]
            ),
            "high_reject_patterns": [
                p for p, c in sorted(reject_by_pattern.items(), key=lambda x: x[1], reverse=True)[:3]
            ],
            "high_reject_categories": [
                c for c, n in sorted(reject_by_category.items(), key=lambda x: x[1], reverse=True)[:3]
            ],
        }

        self.logger.info(
            "Review trends: reject_rate=%.1f%%, top_reason=%s",
            result["reject_rate"],
            list(result["top_reject_reasons"].keys())[:1],
        )
        return result

    def _update_theme_tree(self, analysis: dict[str, Any]) -> None:
        """Update the ``_meta`` section in ``knowledge/theme_tree.yaml``.

        Computes coverage rate, identifies underserved (0-post) leaf themes,
        and picks the top engagement themes from the last 30 days of
        performance records.  Only the ``_meta`` key is written; the rest
        of the YAML structure is left untouched.

        Args:
            analysis: Output of :meth:`_analyze_performance`.
        """
        if not _THEME_TREE_PATH.exists():
            self.logger.warning("theme_tree.yaml not found. Skipping theme tree update.")
            return

        with open(_THEME_TREE_PATH, "r", encoding="utf-8") as f:
            tree: dict[str, Any] = yaml.safe_load(f) or {}

        # --- Collect all leaf themes grouped by top-level category key ---
        category_leaves: dict[str, list[str]] = {}
        all_leaves: list[str] = []
        for top_key, subtree in tree.items():
            if top_key == "_meta":
                continue
            leaves: list[str] = []
            if isinstance(subtree, dict):
                for sub_items in subtree.values():
                    if isinstance(sub_items, list):
                        for item in sub_items:
                            if isinstance(item, str):
                                leaves.append(item)
            elif isinstance(subtree, list):
                for item in subtree:
                    if isinstance(item, str):
                        leaves.append(item)
            category_leaves[top_key] = leaves
            all_leaves.extend(leaves)

        total_leaves = len(all_leaves)
        if total_leaves == 0:
            self.logger.warning("No leaf themes found in theme_tree.yaml. Skipping.")
            return

        # --- Load last 30 days of records from performance.json ---
        perf = self._load_performance()
        records: list[dict[str, Any]] = perf.get("records", [])

        now = datetime.now(JST)
        thirty_days_ago = now - timedelta(days=30)

        recent_records: list[dict[str, Any]] = []
        for r in records:
            posted_at = r.get("posted_at")
            if not posted_at:
                continue
            try:
                dt = datetime.fromisoformat(posted_at)
            except (ValueError, TypeError):
                continue
            if dt >= thirty_days_ago:
                recent_records.append(r)

        # --- Count posts per category (partial match) ---
        cat_post_count: dict[str, int] = defaultdict(int)
        for r in recent_records:
            cat = r.get("category", "")
            if cat:
                cat_post_count[cat] += 1

        # --- Determine which leaves are "covered" ---
        # A leaf is covered if any record's category partially matches the
        # top-level category key that the leaf belongs to AND there is at
        # least one post in that category.
        covered_category_keys: set[str] = set()
        for record_cat in cat_post_count:
            for tree_key in category_leaves:
                if tree_key == record_cat or record_cat.startswith(tree_key + "_"):
                    covered_category_keys.add(tree_key)

        # Per-leaf coverage: a leaf is covered if its parent category
        # received at least one post in the last 30 days.
        covered_leaves: set[str] = set()
        for key in covered_category_keys:
            for leaf in category_leaves.get(key, []):
                covered_leaves.add(leaf)

        covered_count = len(covered_leaves)
        coverage_rate = round(covered_count / total_leaves, 2) if total_leaves else 0.0

        # --- Underserved themes: leaves in categories with 0 posts ---
        underserved: list[str] = [leaf for leaf in all_leaves if leaf not in covered_leaves]
        underserved_themes = underserved[:10]

        # --- Hot themes: top engagement categories mapped to leaves ---
        by_category: dict[str, dict[str, Any]] = analysis.get("by_category", {})
        # Build (category, avg_engagement_rate) sorted desc
        sorted_cats = sorted(
            by_category.items(),
            key=lambda kv: kv[1].get("avg_engagement_rate", 0),
            reverse=True,
        )

        hot_themes: list[dict[str, Any]] = []
        for record_cat, stats in sorted_cats:
            if len(hot_themes) >= 5:
                break
            avg_eng = stats.get("avg_engagement_rate", 0)
            if avg_eng <= 0:
                continue
            # Find a matching leaf from the theme tree
            for tree_key, leaves in category_leaves.items():
                if tree_key == record_cat or record_cat.startswith(tree_key + "_"):
                    for leaf in leaves:
                        if len(hot_themes) >= 5:
                            break
                        # Avoid duplicates
                        if any(h["theme"] == leaf for h in hot_themes):
                            continue
                        hot_themes.append({
                            "theme": leaf,
                            "avg_engagement": round(avg_eng, 1),
                        })
                        break  # one leaf per category match
                    break

        # --- Build _meta section ---
        meta: dict[str, Any] = {
            "coverage_rate": coverage_rate,
            "total_leaves": total_leaves,
            "covered_leaves": covered_count,
            "underserved_themes": underserved_themes,
            "hot_themes": hot_themes,
            "updated_at": now.isoformat(),
        }

        # --- Write back: preserve existing structure, update only _meta ---
        tree["_meta"] = meta

        with open(_THEME_TREE_PATH, "w", encoding="utf-8") as f:
            yaml.dump(
                tree,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )

        self.logger.info(
            "Updated theme_tree.yaml _meta: coverage=%.0f%% (%d/%d), "
            "underserved=%d, hot=%d",
            coverage_rate * 100,
            covered_count,
            total_leaves,
            len(underserved_themes),
            len(hot_themes),
        )

    def _update_schedule_recommendations(
        self, analysis: dict[str, Any], feedback: dict[str, Any]
    ) -> None:
        """Propagate analysis insights into ``config/schedule.yaml``.

        Updates the ``recommended_patterns`` lists for each time slot
        based on the best-performing patterns identified in the analysis
        and feedback.

        Args:
            analysis: Quantitative analysis dict.
            feedback: Claude-generated feedback dict.
        """
        if not _SCHEDULE_PATH.exists():
            self.logger.warning("schedule.yaml not found. Skipping update.")
            return

        with open(_SCHEDULE_PATH, "r", encoding="utf-8") as f:
            schedule: dict[str, Any] = yaml.safe_load(f) or {}

        time_slots: dict[str, Any] = schedule.get("time_slots", {})
        if not time_slots:
            self.logger.warning("No time_slots in schedule.yaml. Skipping.")
            return

        recommended: list[str] = feedback.get("recommended_patterns", [])
        avoid: list[str] = feedback.get("avoid_patterns", [])

        if not recommended:
            self.logger.info("No recommended patterns from feedback. Skipping.")
            return

        # Update each time slot: add recommended patterns, remove avoid patterns
        for slot_name, slot_data in time_slots.items():
            if not isinstance(slot_data, dict):
                continue
            current_patterns: list[str] = slot_data.get("recommended_patterns", [])

            # Remove patterns to avoid
            updated = [p for p in current_patterns if p not in avoid]

            # Add recommended patterns that are not already present
            for pat in recommended:
                if pat not in updated:
                    updated.append(pat)

            slot_data["recommended_patterns"] = updated

        # Add analyst metadata
        schedule["_analyst_updated_at"] = datetime.now(JST).isoformat()

        with open(_SCHEDULE_PATH, "w", encoding="utf-8") as f:
            yaml.dump(
                schedule,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )

        self.logger.info("Updated schedule.yaml with posting-pattern recommendations.")

    # ------------------------------------------------------------------
    # Config loader
    # ------------------------------------------------------------------

    @staticmethod
    def _load_analyst_config() -> dict[str, Any]:
        """Load the ``analyst`` section from settings.yaml."""
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("analyst", {})


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _safe_avg(values: list[float | int]) -> float:
    """Return the arithmetic mean of *values*, or ``0.0`` if empty."""
    if not values:
        return 0.0
    return sum(values) / len(values)
