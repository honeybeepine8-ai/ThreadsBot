"""Researcher agent — collects skincare / beauty topics from YouTube and web trends."""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any
import yaml
from googleapiclient.discovery import build

from agents.base_agent import BaseAgent
from core.constants import JST
from core.logger import get_logger
from services.claude_client import ClaudeClient

logger = get_logger("researcher")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_SEASON_MATRIX_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "season_matrix.yaml"

# Category -> search keywords mapping
_SEARCH_KEYWORDS: dict[str, list[str]] = {
    "skincare_knowledge": ["スキンケア 成分 解説", "美容成分 最新"],
    "skincare_ingredients": ["セラミド 化粧水", "レチノール 使い方", "ナイアシンアミド"],
    "skincare_routine": ["スキンケア ルーティン", "朝 スキンケア 手順"],
    "beauty_trend": ["美容 トレンド 2026", "スキンケア 新商品"],
    "diet_tips": ["ダイエット 食事", "インナーケア 美肌"],
}

# Web trend search keywords (broader discovery)
_WEB_TREND_KEYWORDS: list[str] = [
    "スキンケア トレンド",
    "美容成分 話題",
    "化粧品 新成分",
    "スキンケア バズ",
    "肌悩み 解決",
]

# RSS-like sources for beauty/skincare trends
_WEB_SOURCES: list[dict[str, str]] = [
    {"url": "https://www.cosme.net/beautist/article/new", "name": "@cosme"},
    {"url": "https://maquia.hpplus.jp/skincare/", "name": "MAQUIA"},
    {"url": "https://www.biteki.com/skin-care/", "name": "美的"},
]

# Duplicate detection threshold (keyword overlap ratio)
_DUPLICATE_THRESHOLD = 0.80


class ResearcherAgent(BaseAgent):
    """Collect skincare and beauty content ideas from YouTube and web.

    Workflow:
    1. Identify under-covered theme tree categories.
    2. Search YouTube for recent videos across predefined categories.
    3. Scrape web sources for trending skincare topics.
    4. Extract actionable topics via Claude API analysis.
    5. Deduplicate against the existing research pool.
    6. Append new items to ``research_pool.json``.
    7. Clean up stale items.
    """

    def __init__(self, ctx=None) -> None:
        super().__init__("researcher", ctx=ctx)
        self.claude = ClaudeClient()
        self.youtube = build(
            "youtube",
            "v3",
            developerKey=os.environ.get("YOUTUBE_API_KEY", ""),
        )
        self.config = self._load_researcher_config()

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    def execute(self) -> None:
        """Run the full research pipeline."""
        self.logger.info("Starting research cycle.")

        # 0. Identify coverage gaps from theme tree
        coverage_gaps = self._find_coverage_gaps()
        if coverage_gaps:
            self.logger.info(
                "Theme tree coverage gaps: %d categories need more content.",
                len(coverage_gaps),
            )

        # 1. Search YouTube
        videos = self._search_youtube(priority_categories=coverage_gaps)
        self.logger.info("Found %d videos across all categories.", len(videos))

        # 2. Scrape web trends
        web_topics = self._search_web_trends()
        self.logger.info("Found %d web trend topics.", len(web_topics))

        # 3. Combine all raw sources
        all_raw_topics: list[dict[str, Any]] = []

        if videos:
            youtube_topics = self._extract_topics(videos, coverage_gaps)
            self.logger.info("Extracted %d YouTube topic candidates.", len(youtube_topics))
            all_raw_topics.extend(youtube_topics)

        all_raw_topics.extend(web_topics)

        if not all_raw_topics:
            self.logger.warning("No topics found from any source. Skipping.")
            return

        # 4. Load existing pool and deduplicate
        pool = self.state.load_json("research_pool.json")
        existing_items: list[dict[str, Any]] = pool.get("items", [])

        added = 0
        for topic_item in all_raw_topics:
            if not self._is_duplicate(topic_item.get("topic", ""), existing_items):
                existing_items.append(topic_item)
                added += 1

        self.logger.info(
            "Added %d new topics (%d duplicates skipped).",
            added,
            len(all_raw_topics) - added,
        )

        # 5. Cleanup old items
        existing_items = self._cleanup_old_items(existing_items)

        # 6. Persist
        now_jst = datetime.datetime.now(JST)
        pool["last_updated"] = now_jst.isoformat()
        pool["items"] = existing_items
        self.state.save_json("research_pool.json", pool)

        self.logger.info(
            "Research pool updated. Total items: %d", len(existing_items)
        )

    # ------------------------------------------------------------------
    # Theme Tree coverage analysis
    # ------------------------------------------------------------------

    def _find_coverage_gaps(self) -> list[str]:
        """Identify under-covered theme tree categories from post history.

        Compares the distribution of published posts across categories to
        the full theme tree, and returns categories that have fewer posts
        than the fair share (below average coverage).

        Returns:
            List of category names that need more content.
        """
        try:
            theme_tree = self._load_yaml("knowledge/theme_tree.yaml")
        except FileNotFoundError:
            return []

        # Count leaf nodes per top-level category
        category_leafs: dict[str, int] = {}
        for cat_key, cat_val in theme_tree.items():
            if isinstance(cat_val, dict):
                leaves = 0
                for subcat in cat_val.values():
                    if isinstance(subcat, list):
                        leaves += len(subcat)
                    elif isinstance(subcat, dict):
                        for v in subcat.values():
                            leaves += len(v) if isinstance(v, list) else 1
                category_leafs[cat_key] = max(leaves, 1)
            elif isinstance(cat_val, list):
                category_leafs[cat_key] = len(cat_val)

        if not category_leafs:
            return []

        # Count posts per category in last 30 days
        history = self.state.load_json("post_history.json")
        posts = history.get("posts", [])
        cutoff = datetime.datetime.now(JST) - datetime.timedelta(days=30)

        post_counts: dict[str, int] = {cat: 0 for cat in category_leafs}
        for post in posts:
            try:
                posted_at = datetime.datetime.fromisoformat(post.get("posted_at", ""))
            except (ValueError, TypeError):
                continue
            if posted_at < cutoff:
                continue
            cat = post.get("category", "")
            if cat in post_counts:
                post_counts[cat] += 1

        # Find categories below average coverage ratio
        total_posts = sum(post_counts.values()) or 1
        total_leafs = sum(category_leafs.values())

        gaps: list[str] = []
        for cat, leaf_count in category_leafs.items():
            expected_share = leaf_count / total_leafs
            actual_share = post_counts.get(cat, 0) / total_posts
            if actual_share < expected_share * 0.6:
                gaps.append(cat)

        return gaps

    # ------------------------------------------------------------------
    # Web trend scraping
    # ------------------------------------------------------------------

    def _search_web_trends(self) -> list[dict[str, Any]]:
        """Scrape web sources for trending skincare topics.

        Uses httpx to fetch beauty media sites and extract headline topics.
        Returns research-pool items ready for insertion.
        """
        import httpx

        all_headlines: list[str] = []

        for source in _WEB_SOURCES:
            try:
                resp = httpx.get(
                    source["url"],
                    timeout=15.0,
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; ThreadsBot/1.0)",
                        "Accept-Language": "ja,en;q=0.5",
                    },
                )
                if resp.status_code != 200:
                    self.logger.debug(
                        "Web source '%s' returned status %d", source["name"], resp.status_code
                    )
                    continue

                headlines = self._extract_headlines(resp.text, source["name"])
                all_headlines.extend(headlines)
                self.logger.debug(
                    "Extracted %d headlines from %s", len(headlines), source["name"]
                )

            except Exception as exc:
                self.logger.debug("Web scrape failed for '%s': %s", source["name"], exc)
                continue

        if not all_headlines:
            return []

        # Use Claude to extract structured topics from headlines
        return self._extract_web_topics(all_headlines)

    @staticmethod
    def _extract_headlines(html: str, source_name: str) -> list[str]:
        """Extract article headlines from raw HTML.

        Uses simple regex patterns to find title/heading text.
        """
        headlines: list[str] = []

        # Common patterns for article titles in Japanese beauty sites
        patterns = [
            r'<h[1-3][^>]*>([^<]{10,80})</h[1-3]>',
            r'<a[^>]*title="([^"]{10,80})"',
            r'class="[^"]*title[^"]*"[^>]*>([^<]{10,80})<',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, html)
            for match in matches:
                text = re.sub(r'<[^>]+>', '', match).strip()
                # Filter for skincare/beauty relevance
                if any(kw in text for kw in [
                    "スキンケア", "成分", "美容", "肌", "化粧", "保湿",
                    "セラミド", "レチノール", "ビタミン", "日焼け", "紫外線",
                    "毛穴", "ニキビ", "シミ", "シワ", "乾燥", "敏感肌",
                ]):
                    headlines.append(f"[{source_name}] {text}")

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for h in headlines:
            if h not in seen:
                seen.add(h)
                unique.append(h)

        return unique[:15]  # Cap per source

    def _extract_web_topics(self, headlines: list[str]) -> list[dict[str, Any]]:
        """Use Claude to extract structured topics from web headlines."""
        data_text = "\n".join(headlines)

        season_data = self._load_season_matrix()
        current_month = datetime.datetime.now(JST).month
        month_cfg = season_data.get("months", {}).get(current_month, {})
        season_hint = ""
        if month_cfg:
            season_hint = (
                f"\n\n現在の季節: {month_cfg.get('season', '')} "
                f"({month_cfg.get('theme', '')})"
            )

        prompt = (
            "以下のWeb記事見出しから、Threadsのスキンケア投稿に使えるトレンドネタを抽出してください。"
            "各ネタについて topic, summary, keywords, priority(1-10), category を返してください。"
            "categoryは以下から選択: skincare_knowledge, skincare_ingredients, skincare_routine, beauty_trend, diet_tips"
            f"{season_hint}\n"
            "JSON配列で返してください。JSON配列のみを返し、それ以外のテキストは含めないでください。"
        )

        try:
            raw_response = self.claude.analyze(prompt=prompt, data=data_text)
            topics = self._parse_claude_response(raw_response)
        except Exception as exc:
            self.logger.warning("Web topic extraction via Claude failed: %s", exc)
            return []

        now_jst = datetime.datetime.now(JST)
        date_str = now_jst.strftime("%Y%m%d")

        result: list[dict[str, Any]] = []
        for idx, t in enumerate(topics, start=1):
            category = t.get("category", "beauty_trend")
            if category not in _SEARCH_KEYWORDS:
                category = "beauty_trend"

            result.append({
                "id": f"web_{date_str}_{idx:03d}",
                "source": "web",
                "source_url": "",
                "topic": t.get("topic", ""),
                "summary": t.get("summary", ""),
                "keywords": t.get("keywords", []),
                "category": category,
                "collected_at": now_jst.isoformat(),
                "used": False,
                "priority": int(t.get("priority", 5)),
            })

        return result

    # ------------------------------------------------------------------
    # YouTube search
    # ------------------------------------------------------------------

    def _search_youtube(
        self,
        priority_categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search YouTube Data API v3 for recent skincare / beauty videos.

        When *priority_categories* are specified (from theme tree gaps),
        those categories receive a larger share of the search quota.
        """
        max_results_total: int = self.config.get("youtube_max_results", 20)
        categories: list[str] = self.config.get("categories", list(_SEARCH_KEYWORDS.keys()))

        # Load seasonal adjustments
        season_data = self._load_season_matrix()
        current_month = datetime.datetime.now(JST).month
        month_config: dict[str, Any] = season_data.get("months", {}).get(current_month, {})
        seasonal_keywords: list[str] = month_config.get("extra_keywords", [])
        category_weights: dict[str, float] = month_config.get("category_weights", {})

        if month_config:
            self.logger.info(
                "Seasonal context: month=%d, theme='%s', extra_keywords=%d",
                current_month,
                month_config.get("theme", ""),
                len(seasonal_keywords),
            )

        # Boost weights for under-covered categories
        if priority_categories:
            for cat in priority_categories:
                category_weights[cat] = category_weights.get(cat, 1.0) * 1.5
            self.logger.info(
                "Boosted search weight for coverage-gap categories: %s",
                priority_categories,
            )

        # Compute weighted per-category allocation
        total_weight = sum(category_weights.get(cat, 1.0) for cat in categories)
        if total_weight <= 0:
            total_weight = float(len(categories)) or 1.0
        weighted_allocation: dict[str, int] = {}
        for cat in categories:
            weight = category_weights.get(cat, 1.0)
            allocated = max(1, int(max_results_total * weight / total_weight))
            weighted_allocation[cat] = allocated

        published_after = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=7)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        all_videos: list[dict[str, Any]] = []

        for category in categories:
            keywords = _SEARCH_KEYWORDS.get(category, [])
            if not keywords:
                self.logger.debug("No keywords for category '%s', skipping.", category)
                continue

            cat_max = weighted_allocation.get(category, max(1, max_results_total // len(categories)))
            for keyword in keywords:
                try:
                    response = (
                        self.youtube.search()
                        .list(
                            q=keyword,
                            part="snippet",
                            type="video",
                            order="date",
                            maxResults=cat_max,
                            publishedAfter=published_after,
                            relevanceLanguage="ja",
                        )
                        .execute()
                    )
                except Exception as exc:
                    self.logger.error(
                        "YouTube search failed for '%s': %s", keyword, exc
                    )
                    continue

                for item in response.get("items", []):
                    snippet = item.get("snippet", {})
                    video = {
                        "video_id": item["id"]["videoId"],
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "channel_title": snippet.get("channelTitle", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "category": category,
                    }
                    all_videos.append(video)

        # Search seasonal keywords (not tied to a specific category)
        for keyword in seasonal_keywords:
            try:
                response = (
                    self.youtube.search()
                    .list(
                        q=keyword,
                        part="snippet",
                        type="video",
                        order="date",
                        maxResults=2,
                        publishedAfter=published_after,
                        relevanceLanguage="ja",
                    )
                    .execute()
                )
            except Exception as exc:
                self.logger.error(
                    "YouTube search failed for seasonal '%s': %s", keyword, exc
                )
                continue

            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                video = {
                    "video_id": item["id"]["videoId"],
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "channel_title": snippet.get("channelTitle", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "category": self._guess_seasonal_category(keyword),
                }
                all_videos.append(video)

        # Deduplicate by video_id (same video may appear in multiple queries)
        seen_ids: set[str] = set()
        unique_videos: list[dict[str, Any]] = []
        for v in all_videos:
            if v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                unique_videos.append(v)

        return unique_videos

    # ------------------------------------------------------------------
    # Topic extraction via Claude
    # ------------------------------------------------------------------

    def _extract_topics(
        self,
        videos: list[dict[str, Any]],
        coverage_gaps: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Use Claude to extract actionable Threads post topics from videos."""
        # Build a textual summary of all videos grouped by category
        lines: list[str] = []
        for v in videos:
            lines.append(
                f"[{v['category']}] {v['title']}\n"
                f"  チャンネル: {v['channel_title']}\n"
                f"  説明: {v['description'][:300]}\n"
                f"  URL: https://youtube.com/watch?v={v['video_id']}\n"
            )

        data_text = "\n".join(lines)

        # Add seasonal context hint
        season_data = self._load_season_matrix()
        current_month = datetime.datetime.now(JST).month
        month_cfg = season_data.get("months", {}).get(current_month, {})
        season_hint = ""
        if month_cfg:
            season_hint = (
                f"\n\n現在の季節コンテキスト: {month_cfg.get('season', '')} "
                f"({month_cfg.get('theme', '')})\n"
                f"この季節に特に需要が高いテーマを優先してください。"
            )

        # Add coverage gap hint
        gap_hint = ""
        if coverage_gaps:
            gap_hint = (
                f"\n\n以下のカテゴリのネタが不足しています。優先的に抽出してください: "
                f"{', '.join(coverage_gaps)}"
            )

        prompt = (
            "以下のYouTube動画の情報から、Threadsのスキンケア投稿に使えるネタを抽出してください。"
            "各ネタについて topic, summary, keywords, priority(1-10) を返してください。"
            f"{season_hint}{gap_hint}\n"
            "JSON配列で返してください。\n"
            "例: [{\"topic\": \"...\", \"summary\": \"...\", \"keywords\": [\"...\"], \"priority\": 8}]\n"
            "JSON配列のみを返し、それ以外のテキストは含めないでください。"
        )

        raw_response = self.claude.analyze(prompt=prompt, data=data_text)

        # Parse Claude response into structured data
        topics = self._parse_claude_response(raw_response)

        now_jst = datetime.datetime.now(JST)
        date_str = now_jst.strftime("%Y%m%d")

        result: list[dict[str, Any]] = []
        for idx, t in enumerate(topics, start=1):
            item_id = f"res_{date_str}_{idx:03d}"
            matched_category = self._guess_category(t, videos)

            result.append(
                {
                    "id": item_id,
                    "source": "youtube",
                    "source_url": self._guess_source_url(t, videos),
                    "topic": t.get("topic", ""),
                    "summary": t.get("summary", ""),
                    "keywords": t.get("keywords", []),
                    "category": matched_category,
                    "collected_at": now_jst.isoformat(),
                    "used": False,
                    "priority": int(t.get("priority", 5)),
                }
            )

        return result

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------

    def _is_duplicate(self, topic: str, existing_items: list[dict[str, Any]]) -> bool:
        """Check whether *topic* is a duplicate of any existing pool item."""
        if not topic:
            return True

        topic_words = set(self._tokenize(topic))
        if not topic_words:
            return True

        for item in existing_items:
            existing_topic = item.get("topic", "")

            if topic == existing_topic:
                return True

            existing_words = set(self._tokenize(existing_topic))
            if not existing_words:
                continue

            intersection = topic_words & existing_words
            smaller_len = min(len(topic_words), len(existing_words))
            if smaller_len == 0:
                continue

            overlap = len(intersection) / smaller_len
            if overlap >= _DUPLICATE_THRESHOLD:
                self.logger.debug(
                    "Duplicate detected (%.0f%%): '%s' ≈ '%s'",
                    overlap * 100,
                    topic,
                    existing_topic,
                )
                return True

        return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_old_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove stale items from the research pool.

        Rules:
        - ``used=True`` **and** older than 7 days  -> remove.
        - ``used=False`` **and** older than 30 days -> remove.
        """
        now = datetime.datetime.now(JST)
        kept: list[dict[str, Any]] = []

        for item in items:
            collected_str = item.get("collected_at", "")
            try:
                collected_at = datetime.datetime.fromisoformat(collected_str)
            except (ValueError, TypeError):
                kept.append(item)
                continue

            age = now - collected_at

            if item.get("used") and age > datetime.timedelta(days=7):
                self.logger.debug("Removing used item older than 7d: %s", item.get("id"))
                continue

            if not item.get("used") and age > datetime.timedelta(days=30):
                self.logger.debug("Removing unused item older than 30d: %s", item.get("id"))
                continue

            kept.append(item)

        removed = len(items) - len(kept)
        if removed:
            self.logger.info("Cleaned up %d stale items.", removed)

        return kept

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_season_matrix(self) -> dict[str, Any]:
        """Load the seasonal matrix from knowledge/season_matrix.yaml."""
        try:
            return self._load_yaml("knowledge/season_matrix.yaml")
        except FileNotFoundError:
            logger.warning("Season matrix not found.")
            return {}

    @staticmethod
    def _guess_seasonal_category(keyword: str) -> str:
        """Guess the best category for a seasonal keyword."""
        kw = keyword.lower()
        if any(w in kw for w in ["成分", "セラミド", "ビタミン", "レチノール", "美白"]):
            return "skincare_ingredients"
        if any(w in kw for w in ["ルーティン", "手順", "切り替え"]):
            return "skincare_routine"
        if any(w in kw for w in ["トレンド", "新商品", "ベストコスメ", "コフレ"]):
            return "beauty_trend"
        if any(w in kw for w in ["ダイエット", "インナーケア"]):
            return "diet_tips"
        return "skincare_knowledge"

    def _load_researcher_config(self) -> dict[str, Any]:
        """Load the ``researcher`` section from *settings.yaml*."""
        cfg = self._load_yaml("config/settings.yaml")
        return cfg.get("researcher", {})

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Naively tokenize Japanese / mixed text into words."""
        tokens = re.split(r"[\s、。・,.\-/()（）「」【】\[\]]+", text)
        return [t for t in tokens if len(t) >= 2]

    @staticmethod
    def _parse_claude_response(raw: str) -> list[dict[str, Any]]:
        """Extract a JSON array from Claude's response text."""
        text = raw.strip()

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            logger.warning("Could not locate JSON array in Claude response.")
            return []

        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse Claude JSON: %s", exc)

        return []

    @staticmethod
    def _guess_category(
        topic_item: dict[str, Any], videos: list[dict[str, Any]]
    ) -> str:
        """Best-effort category assignment for an extracted topic."""
        best_category = "skincare_knowledge"
        best_score = 0

        for v in videos:
            title_lower = v["title"].lower()
            score = sum(1 for kw in topic_item.get("keywords", []) if kw.lower() in title_lower)
            if score > best_score:
                best_score = score
                best_category = v["category"]

        return best_category

    @staticmethod
    def _guess_source_url(
        topic_item: dict[str, Any], videos: list[dict[str, Any]]
    ) -> str:
        """Best-effort source URL assignment for an extracted topic."""
        best_url = (
            f"https://youtube.com/watch?v={videos[0]['video_id']}" if videos else ""
        )
        best_score = 0

        for v in videos:
            title_lower = v["title"].lower()
            score = sum(1 for kw in topic_item.get("keywords", []) if kw.lower() in title_lower)
            if score > best_score:
                best_score = score
                best_url = f"https://youtube.com/watch?v={v['video_id']}"

        return best_url
