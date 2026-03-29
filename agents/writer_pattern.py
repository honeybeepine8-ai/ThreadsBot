"""WriterPatternMixin — pattern and time-slot selection logic."""

from __future__ import annotations

import datetime
import random
from typing import Any

from core.boost import get_viral_pattern_config
from core.constants import JST


class WriterPatternMixin:
    """Mixin providing pattern selection and time-slot helpers for WriterAgent."""

    # These are re-imported from the main writer module at class composition time
    # via WriterAgent's MRO, but we also reference the canonical constants directly.

    def _select_pattern(self) -> str:
        """Select a posting pattern that has not been used in recent posts.

        Strategy:
        1. Load the most recent posts from ``post_history.json``.
        2. Exclude patterns used in the last *recent_pattern_block* posts.
        3. Prefer patterns recommended by ``schedule.yaml`` for the current
           time-of-day slot.
        4. Add randomness so the selection is not deterministic.

        Returns:
            A pattern name string (e.g. ``"コメント誘導型"``).
        """
        from agents.writer_constants import _ALL_PATTERNS

        # Gather recently used patterns
        history_data: dict[str, Any] = self.state.load_json("post_history.json")
        posts: list[dict[str, Any]] = history_data.get("posts", [])
        recent_patterns: list[str] = [
            p["pattern"] for p in posts if p.get("pattern")
        ][-self.recent_pattern_block:]

        # Also check queued posts (not yet posted but scheduled)
        queue_data: dict[str, Any] = self.state.load_json("post_queue.json")
        queued: list[dict[str, Any]] = queue_data.get("queue", [])
        for item in queued:
            if item.get("status") == "pending" and item.get("pattern"):
                recent_patterns.append(item["pattern"])
        recent_patterns = recent_patterns[-self.recent_pattern_block:]

        # Exclude recently used
        available = [p for p in _ALL_PATTERNS if p not in recent_patterns]
        if not available:
            # Fallback: allow all
            available = list(_ALL_PATTERNS)

        # QA answer prioritisation: if follower questions exist in pool, boost
        if "フォロワー質問回答型" in available:
            pool_data = self.state.load_json("research_pool.json")
            has_questions = any(
                item.get("source") == "follower_question" and not item.get("used")
                for item in pool_data.get("items", [])
            )
            if has_questions and random.random() < 0.6:
                return "フォロワー質問回答型"

        # Thread ratio enforcement: boost ツリー展開型 selection when under target
        if (
            self.thread_ratio > 0
            and "ツリー展開型" in available
        ):
            current_ratio = self._get_current_thread_ratio()
            if current_ratio < self.thread_ratio:
                deficit = self.thread_ratio - current_ratio
                boost_prob = min(deficit * 2, 0.8)
                if random.random() < boost_prob:
                    return "ツリー展開型"

        # Boost mode: prioritize viral patterns
        viral_boost, viral_patterns = get_viral_pattern_config()
        if viral_boost > 0 and viral_patterns:
            viral_available = [p for p in available if p in viral_patterns]
            if viral_available and random.random() < viral_boost:
                return random.choice(viral_available)

        # Performance-based optimization (4-5): deprioritize avoid_patterns
        feedback = self._audience_data.get("feedback", {})
        avoid = set(feedback.get("avoid_patterns", []))
        analyst_recommended = feedback.get("recommended_patterns", [])

        # Remove avoid_patterns from available when alternatives exist
        if avoid:
            non_avoided = [p for p in available if p not in avoid]
            if non_avoided:
                available = non_avoided

        # Determine recommended patterns for current time slot
        recommended = self._get_recommended_patterns_for_now()
        # Merge analyst recommendations from audience.json
        all_recommended = list(dict.fromkeys(recommended + analyst_recommended))

        # Prefer recommended patterns with weighted random
        preferred = [p for p in available if p in all_recommended]
        if preferred:
            # 70% chance to pick from preferred, 30% from any available
            if random.random() < 0.7:
                return random.choice(preferred)

        return random.choice(available)

    def _get_current_thread_ratio(self) -> float:
        """Compute the ratio of thread-format posts among recent posts.

        Considers both posted history and pending queue items to give an
        accurate view of the upcoming content mix.
        """
        history_data = self.state.load_json("post_history.json")
        posts = history_data.get("posts", [])
        queue_data = self.state.load_json("post_queue.json")
        pending = [
            q for q in queue_data.get("queue", [])
            if q.get("status") == "pending"
        ]

        # Look at the last 20 posts + all pending
        recent = posts[-20:] + pending
        if not recent:
            return 0.0

        thread_count = sum(
            1 for p in recent
            if p.get("pattern") == "ツリー展開型"
        )
        return thread_count / len(recent)

    def _get_recommended_patterns_for_now(self) -> list[str]:
        """Return the recommended patterns for the current time-of-day slot."""
        now = datetime.datetime.now(JST)
        current_hour = now.hour

        time_slots: dict[str, Any] = self.schedule_config.get("time_slots", {})
        best_slot: dict[str, Any] | None = None
        best_distance = 999

        for _slot_name, slot_cfg in time_slots.items():
            base_time_str: str = slot_cfg.get("base_time", "12:00")
            parts = base_time_str.split(":")
            if len(parts) < 2:
                continue
            slot_hour = int(parts[0])
            distance = abs(current_hour - slot_hour)
            if distance < best_distance:
                best_distance = distance
                best_slot = slot_cfg

        if best_slot:
            return best_slot.get("recommended_patterns", [])
        return []

    # ==================================================================
    # Time-slot selection
    # ==================================================================

    def _select_time_slot(self) -> str:
        """Pick an available time slot and return an ISO datetime string.

        Rules:
        - Use ``schedule.yaml`` to find today's time slots.
        - Consider the day-of-week post count limit.
        - Avoid slots that are already assigned in the queue.
        - Apply ``jitter_minutes`` randomness around ``base_time``.
        - If no today slot is available, fall back to tomorrow.

        Returns:
            ISO-format datetime string with JST offset, e.g.
            ``"2026-03-22T12:15:00+09:00"``.
        """
        from agents.writer_constants import _WEEKDAY_NAMES

        now = datetime.datetime.now(JST)
        today_weekday = _WEEKDAY_NAMES[now.weekday()]

        # Gather already-scheduled times in the queue
        queue_data: dict[str, Any] = self.state.load_json("post_queue.json")
        queued: list[dict[str, Any]] = queue_data.get("queue", [])
        scheduled_slots: set[str] = set()
        for item in queued:
            if item.get("status") == "pending" and item.get("scheduled_at"):
                # Store slot label (morning/lunch/evening/night) by hour proximity
                try:
                    dt = datetime.datetime.fromisoformat(item["scheduled_at"])
                    scheduled_slots.add(f"{dt.date().isoformat()}_{dt.hour}")
                except (ValueError, TypeError):
                    pass

        # Try today first, then tomorrow
        for day_offset in range(2):
            target_date = now.date() + datetime.timedelta(days=day_offset)
            target_weekday = _WEEKDAY_NAMES[target_date.weekday()]

            # How many posts for this day?
            daily_counts: dict[str, int] = self.schedule_config.get("daily_post_count", {})
            max_posts_today = daily_counts.get(target_weekday, 3)

            # Count how many posts are already queued for this date
            queued_for_date = sum(
                1 for item in queued
                if item.get("status") == "pending"
                and item.get("scheduled_at", "").startswith(target_date.isoformat())
            )
            if queued_for_date >= max_posts_today:
                continue

            # Iterate time slots and find an available one
            time_slots: dict[str, Any] = self.schedule_config.get("time_slots", {})
            candidates: list[tuple[str, dict[str, Any]]] = []

            for slot_name, slot_cfg in time_slots.items():
                # Check day restriction (e.g. "night" slot only on wed/thu)
                days_only: list[str] | None = slot_cfg.get("days_only")
                if days_only and target_weekday not in days_only:
                    continue

                base_time_str: str = slot_cfg.get("base_time", "12:00")
                parts = base_time_str.split(":")
                if len(parts) < 2:
                    continue
                base_hour, base_minute = int(parts[0]), int(parts[1])

                # Skip if a slot near this hour is already taken
                slot_key = f"{target_date.isoformat()}_{base_hour}"
                if slot_key in scheduled_slots:
                    continue

                # For today, skip slots whose time has already passed
                if day_offset == 0:
                    slot_dt = datetime.datetime.combine(
                        target_date,
                        datetime.time(base_hour, base_minute),
                        tzinfo=JST,
                    )
                    if slot_dt < now:
                        continue

                candidates.append((slot_name, slot_cfg))

            if not candidates:
                continue

            # Pick one candidate (prefer affiliate slots if applicable)
            slot_name, slot_cfg = random.choice(candidates)
            return self._apply_jitter(target_date, slot_cfg)

        # Ultimate fallback: tomorrow noon
        tomorrow = now.date() + datetime.timedelta(days=1)
        fallback = datetime.datetime.combine(
            tomorrow,
            datetime.time(12, 15),
            tzinfo=JST,
        )
        return fallback.isoformat()

    @staticmethod
    def _apply_jitter(target_date: datetime.date, slot_cfg: dict[str, Any]) -> str:
        """Compute a jittered datetime for a given slot config.

        Args:
            target_date: The date to schedule on.
            slot_cfg: A slot dict from ``schedule.yaml`` with ``base_time``
                and ``jitter_minutes``.

        Returns:
            ISO-format datetime string with JST offset.
        """
        base_time_str: str = slot_cfg.get("base_time", "12:00")
        jitter: int = slot_cfg.get("jitter_minutes", 15)
        parts = base_time_str.split(":")
        base_hour = int(parts[0])
        base_minute = int(parts[1]) if len(parts) >= 2 else 0

        base_dt = datetime.datetime.combine(
            target_date,
            datetime.time(base_hour, base_minute),
            tzinfo=JST,
        )
        offset_minutes = random.randint(-jitter, jitter)
        result = base_dt + datetime.timedelta(minutes=offset_minutes)
        return result.isoformat()

    def _is_pr_slot(self, scheduled_at: str) -> bool:
        """Determine whether the given time slot should carry an affiliate comment.

        This checks whether the slot falls within a ``prefer_affiliate: true``
        window in ``schedule.yaml`` **and** a random draw based on
        ``pr_ratio`` succeeds.

        Args:
            scheduled_at: ISO-format datetime string.

        Returns:
            ``True`` if this post should include an affiliate comment.
        """
        try:
            dt = datetime.datetime.fromisoformat(scheduled_at)
        except (ValueError, TypeError):
            return False

        post_hour = dt.hour

        # Check if any slot marked prefer_affiliate matches this hour
        time_slots: dict[str, Any] = self.schedule_config.get("time_slots", {})
        in_affiliate_slot = False
        for _name, slot_cfg in time_slots.items():
            if not slot_cfg.get("prefer_affiliate", False):
                continue
            base_time_str: str = slot_cfg.get("base_time", "12:00")
            jitter: int = slot_cfg.get("jitter_minutes", 15)
            parts = base_time_str.split(":")
            base_hour = int(parts[0]) if parts else 12
            if abs(post_hour - base_hour) * 60 <= jitter + 30:
                in_affiliate_slot = True
                break

        if in_affiliate_slot:
            return random.random() < self.pr_ratio

        return False
