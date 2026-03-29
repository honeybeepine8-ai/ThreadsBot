"""Boost mode utilities — detect and apply initial growth overrides."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from core.logger import get_logger

logger = get_logger("boost")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


def load_boost_config() -> dict[str, Any]:
    """Load the ``boost_mode`` section from settings.yaml."""
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("boost_mode", {})


def is_boost_active() -> bool:
    """Return True if boost mode is currently active."""
    boost = load_boost_config()
    if not boost.get("enabled"):
        return False

    try:
        start = date.fromisoformat(boost["start_date"])
        end = start + timedelta(days=boost.get("duration_days", 14))
        return start <= date.today() < end
    except (KeyError, ValueError):
        return False


def get_effective_safety_config(base_config: dict[str, Any]) -> dict[str, Any]:
    """Return safety config with boost overrides applied if active.

    Args:
        base_config: The base ``safety`` dict from settings.yaml.

    Returns:
        Merged config dict.
    """
    if not is_boost_active():
        return base_config

    boost = load_boost_config()
    overrides = boost.get("overrides", {}).get("safety", {})
    if overrides:
        logger.info("Boost mode ACTIVE — applying safety overrides: %s", overrides)
        return {**base_config, **overrides}
    return base_config


def get_effective_schedule(base_schedule: dict[str, Any]) -> dict[str, Any]:
    """Return schedule config with boost overrides applied if active.

    Args:
        base_schedule: The base schedule.yaml dict.

    Returns:
        Merged schedule dict.
    """
    if not is_boost_active():
        return base_schedule

    boost = load_boost_config()
    overrides = boost.get("overrides", {}).get("schedule", {})
    if overrides:
        logger.info("Boost mode ACTIVE — applying schedule overrides")
        merged = dict(base_schedule)
        if "daily_post_count" in overrides:
            merged["daily_post_count"] = overrides["daily_post_count"]
        return merged
    return base_schedule


def get_effective_writer_config(base_writer: dict[str, Any]) -> dict[str, Any]:
    """Return writer config with boost overrides applied if active.

    Args:
        base_writer: The base ``writer`` dict from settings.yaml.

    Returns:
        Merged config dict.
    """
    if not is_boost_active():
        return base_writer

    boost = load_boost_config()
    overrides = boost.get("overrides", {}).get("writer", {})
    if overrides:
        logger.info("Boost mode ACTIVE — applying writer overrides: %s", overrides)
        return {**base_writer, **overrides}
    return base_writer


def get_viral_pattern_config() -> tuple[float, list[str]]:
    """Return (viral_boost_rate, viral_pattern_names) if boost is active.

    Returns:
        (0.0, []) when boost is inactive.
    """
    if not is_boost_active():
        return 0.0, []

    boost = load_boost_config()
    content_cfg = boost.get("overrides", {}).get("content", {})
    return (
        content_cfg.get("viral_pattern_boost", 0.0),
        content_cfg.get("viral_patterns", []),
    )
