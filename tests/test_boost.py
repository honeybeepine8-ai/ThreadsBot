"""Tests for core.boost — boost mode detection and config overrides."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from core.boost import (
    get_effective_safety_config,
    get_effective_schedule,
    get_effective_writer_config,
    get_viral_pattern_config,
    is_boost_active,
)


def _make_boost_config(
    enabled: bool = True,
    start_offset_days: int = -3,
    duration_days: int = 14,
) -> dict:
    """Helper: build a boost_mode config dict with controllable dates."""
    start_date = (date.today() + timedelta(days=start_offset_days)).isoformat()
    return {
        "enabled": enabled,
        "start_date": start_date,
        "duration_days": duration_days,
        "overrides": {
            "safety": {
                "max_daily_posts": 15,
                "min_post_interval_minutes": 60,
            },
            "writer": {
                "pr_ratio": 0.0,
                "profile_cta_rate": 0.0,
            },
            "schedule": {
                "daily_post_count": {
                    "monday": 4, "tuesday": 4, "wednesday": 5,
                    "thursday": 5, "friday": 4, "saturday": 3, "sunday": 3,
                },
            },
            "content": {
                "viral_pattern_boost": 0.7,
                "viral_patterns": ["コメント誘導型", "あるある共感型", "反常識型"],
            },
        },
    }


# ====================================================================
# is_boost_active
# ====================================================================


class TestIsBoostActive:

    def test_active_within_period(self) -> None:
        boost = _make_boost_config(enabled=True, start_offset_days=-3)
        with patch("core.boost.load_boost_config", return_value=boost):
            assert is_boost_active() is True

    def test_inactive_when_disabled(self) -> None:
        boost = _make_boost_config(enabled=False, start_offset_days=-3)
        with patch("core.boost.load_boost_config", return_value=boost):
            assert is_boost_active() is False

    def test_inactive_after_expiry(self) -> None:
        boost = _make_boost_config(enabled=True, start_offset_days=-30, duration_days=14)
        with patch("core.boost.load_boost_config", return_value=boost):
            assert is_boost_active() is False

    def test_inactive_before_start(self) -> None:
        boost = _make_boost_config(enabled=True, start_offset_days=5)
        with patch("core.boost.load_boost_config", return_value=boost):
            assert is_boost_active() is False

    def test_inactive_on_last_day(self) -> None:
        """Boost ends on start_date + duration_days (exclusive)."""
        boost = _make_boost_config(enabled=True, start_offset_days=-14, duration_days=14)
        with patch("core.boost.load_boost_config", return_value=boost):
            assert is_boost_active() is False


# ====================================================================
# get_effective_safety_config
# ====================================================================


class TestEffectiveSafetyConfig:

    def test_overrides_applied_during_boost(self) -> None:
        boost = _make_boost_config(enabled=True, start_offset_days=-3)
        base = {"max_daily_posts": 10, "min_post_interval_minutes": 90}
        with patch("core.boost.load_boost_config", return_value=boost):
            with patch("core.boost.is_boost_active", return_value=True):
                result = get_effective_safety_config(base)
        assert result["max_daily_posts"] == 15
        assert result["min_post_interval_minutes"] == 60

    def test_base_config_when_no_boost(self) -> None:
        base = {"max_daily_posts": 10, "min_post_interval_minutes": 90}
        with patch("core.boost.is_boost_active", return_value=False):
            result = get_effective_safety_config(base)
        assert result == base


# ====================================================================
# get_effective_schedule
# ====================================================================


class TestEffectiveSchedule:

    def test_schedule_override_during_boost(self) -> None:
        boost = _make_boost_config(enabled=True, start_offset_days=-3)
        base = {"daily_post_count": {"monday": 3}, "time_slots": {}}
        with patch("core.boost.load_boost_config", return_value=boost):
            with patch("core.boost.is_boost_active", return_value=True):
                result = get_effective_schedule(base)
        assert result["daily_post_count"]["monday"] == 4
        assert result["daily_post_count"]["wednesday"] == 5

    def test_base_schedule_when_inactive(self) -> None:
        base = {"daily_post_count": {"monday": 3}}
        with patch("core.boost.is_boost_active", return_value=False):
            result = get_effective_schedule(base)
        assert result == base


# ====================================================================
# get_effective_writer_config
# ====================================================================


class TestEffectiveWriterConfig:

    def test_writer_override_during_boost(self) -> None:
        boost = _make_boost_config(enabled=True, start_offset_days=-3)
        base = {"pr_ratio": 0.3, "profile_cta_rate": 0.35}
        with patch("core.boost.load_boost_config", return_value=boost):
            with patch("core.boost.is_boost_active", return_value=True):
                result = get_effective_writer_config(base)
        assert result["pr_ratio"] == 0.0
        assert result["profile_cta_rate"] == 0.0

    def test_base_writer_when_inactive(self) -> None:
        base = {"pr_ratio": 0.3, "profile_cta_rate": 0.35}
        with patch("core.boost.is_boost_active", return_value=False):
            result = get_effective_writer_config(base)
        assert result == base


# ====================================================================
# get_viral_pattern_config
# ====================================================================


class TestViralPatternConfig:

    def test_returns_patterns_during_boost(self) -> None:
        boost = _make_boost_config(enabled=True, start_offset_days=-3)
        with patch("core.boost.load_boost_config", return_value=boost):
            with patch("core.boost.is_boost_active", return_value=True):
                rate, patterns = get_viral_pattern_config()
        assert rate == 0.7
        assert "コメント誘導型" in patterns
        assert len(patterns) == 3

    def test_returns_zero_when_inactive(self) -> None:
        with patch("core.boost.is_boost_active", return_value=False):
            rate, patterns = get_viral_pattern_config()
        assert rate == 0.0
        assert patterns == []
