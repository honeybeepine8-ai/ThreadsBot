"""Researcher agent — collects skincare / beauty topics from YouTube."""

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

# Duplicate detection threshold (keyword overlap ratio)
_DUPLICATE_THRESHOLD = 0.80


class ResearcherAgent(BaseAgent):
    """Collect skincare and beauty content ideas from YouTube.

    Workflow:
    1. Search YouTube for recent videos across predefined categories.
    2. Extract actionable topics via Claude API analysis.
    3. Deduplicate against the existing research pool.
    4. Append new items to ``research_pool.json``.
    5. Clean up stale items.
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

        # 1. Search YouTube
        videos = self._search_youtube()
        if not videos:
            self.logger.warning("No videos found. Skipping extraction.")
            return

        self.logger.info("Found %d videos across all categories.", len(videos))

        # 2. Extract topics via Claude
        new_topics = self._extract_topics(videos)
        self.logger.info("Extracted %d topic candidates.", len(new_topics))

        # 3. Load existing pool and deduplicate
        pool = self.state.load_json("research_pool.json")
        existing_items: list[dict[str, Any]] = pool.get("items", [])

        added = 0
        for topic_item in new_topics:
            if not self._is_duplicate(topic_item.get("topic", ""), existing_items):
                existing_items.append(topic_item)
                added += 1

        self.logger.info(
            "Added %d new topics (%d duplicates skipped).",
            added,
            len(new_topics) - added,
        )

        # 4. Cleanup old items
        existing_items = self._cleanup_old_items(existing_items)

        # 5. Persist
        now_jst = datetime.datetime.now(JST)
        pool["last_updated"] = now_jst.isoformat()
        pool["items"] = existing_items
        self.state.save_json("research_pool.json", pool)

        self.logger.info(
            "Research pool updated. Total items: %d", len(existing_items)
        )

    # ------------------------------------------------------------------
    # YouTube search
    # ------------------------------------------------------------------

    def _search_youtube(self) -> list[dict[str, Any]]:
        """Search YouTube Data API v3 for recent skincare / beauty videos.

        Searches are performed per category using predefined keywords.
        Seasonal keywords are added based on the current month via
        ``knowledge/season_matrix.yaml``, and per-category allocations
        are weighted according to seasonal relevance.

        Returns:
            List of dicts with keys: ``video_id``, ``title``,
            ``description``, ``channel_title``, ``published_at``,
            ``category``.
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

    def _extract_topics(self, videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Use Claude to extract actionable Threads post topics from videos.

        Args:
            videos: List of video metadata dicts from :meth:`_search_youtube`.

        Returns:
            List of research-pool items ready for insertion.
        """
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

        prompt = (
            "以下のYouTube動画の情報から、Threadsのスキンケア投稿に使えるネタを抽出してください。"
            "各ネタについて topic, summary, keywords, priority(1-10) を返してください。"
            f"{season_hint}\n"
            "JSON配列で返してください。\n"
            "例: [{\"topic\": \"...\", \"summary\": \"...\", \"keywords\": [\"...\"], \"priority\": 8}]\n"
            "JSON配列のみを返し、それ以外のテキストは含めないでください。"
        )

        raw_response = self.claude.analyze(prompt=prompt, data=data_text)

        # Parse Claude response into structured data
        topics = self._parse_claude_response(raw_response)

        # Build a lookup: category by video title substring
        category_map: dict[str, str] = {}
        url_map: dict[str, str] = {}
        for v in videos:
            # Use first 20 chars of title as a rough key
            category_map[v["title"][:20]] = v["category"]
            url_map[v["title"][:20]] = f"https://youtube.com/watch?v={v['video_id']}"

        now_jst = datetime.datetime.now(JST)
        date_str = now_jst.strftime("%Y%m%d")

        result: list[dict[str, Any]] = []
        for idx, t in enumerate(topics, start=1):
            item_id = f"res_{date_str}_{idx:03d}"

            # Try to match category from extracted topic keywords
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
        """Check whether *topic* is a duplicate of any existing pool item.

        Uses keyword overlap ratio for a lightweight comparison.  An item is
        considered duplicate if:
        - The topic string is an exact match, **or**
        - 80 %+ of the words overlap with an existing topic.

        Args:
            topic: The candidate topic string.
            existing_items: Current items in the research pool.

        Returns:
            ``True`` if a duplicate was detected.
        """
        if not topic:
            return True

        topic_words = set(self._tokenize(topic))
        if not topic_words:
            return True

        for item in existing_items:
            existing_topic = item.get("topic", "")

            # Exact match
            if topic == existing_topic:
                return True

            existing_words = set(self._tokenize(existing_topic))
            if not existing_words:
                continue

            # Keyword overlap ratio (Jaccard-like but using the smaller set)
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

        Args:
            items: Current pool items.

        Returns:
            Filtered list with stale items removed.
        """
        now = datetime.datetime.now(JST)
        kept: list[dict[str, Any]] = []

        for item in items:
            collected_str = item.get("collected_at", "")
            try:
                collected_at = datetime.datetime.fromisoformat(collected_str)
            except (ValueError, TypeError):
                # Cannot parse — keep the item to be safe
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
            logger.warning("Season matrix not found for account '%s'", self.ctx.account_id)
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
        """Naively tokenize Japanese / mixed text into words.

        Splits on whitespace and common punctuation, filtering out very
        short tokens.  This is intentionally simple — a production system
        would use MeCab or similar.
        """
        tokens = re.split(r"[\s、。・,.\-/()（）「」【】\[\]]+", text)
        return [t for t in tokens if len(t) >= 2]

    @staticmethod
    def _parse_claude_response(raw: str) -> list[dict[str, Any]]:
        """Extract a JSON array from Claude's response text.

        Handles cases where the response is wrapped in markdown fences.

        Args:
            raw: Raw text from :meth:`ClaudeClient.analyze`.

        Returns:
            Parsed list of topic dicts.  Returns ``[]`` on failure.
        """
        text = raw.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        # Find the JSON array
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
        """Best-effort category assignment for an extracted topic.

        Compares the topic's keywords against each video's title to find
        the most likely source category.  Falls back to
        ``"skincare_knowledge"`` when no match is found.
        """
        topic_text = (
            topic_item.get("topic", "") + " " + " ".join(topic_item.get("keywords", []))
        ).lower()

        best_category = "skincare_knowledge"
        best_score = 0

        for v in videos:
            title_lower = v["title"].lower()
            # Count how many topic keywords appear in the video title
            score = sum(1 for kw in topic_item.get("keywords", []) if kw.lower() in title_lower)
            if score > best_score:
                best_score = score
                best_category = v["category"]

        return best_category

    @staticmethod
    def _guess_source_url(
        topic_item: dict[str, Any], videos: list[dict[str, Any]]
    ) -> str:
        """Best-effort source URL assignment for an extracted topic.

        Uses a keyword-matching heuristic similar to
        :meth:`_guess_category`.  Falls back to the first video's URL.
        """
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
