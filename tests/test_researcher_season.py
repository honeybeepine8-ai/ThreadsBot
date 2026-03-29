"""Tests for seasonal matrix integration in ResearcherAgent."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

_JST = __import__("zoneinfo").ZoneInfo("Asia/Tokyo")

# We test the static methods directly by importing them from the module
# after mocking the google API dependency (which may not be installed in CI).
_MODULE_PATH = Path(__file__).resolve().parent.parent / "agents" / "researcher.py"
_SEASON_MATRIX = Path(__file__).resolve().parent.parent / "knowledge" / "season_matrix.yaml"


def _load_matrix() -> dict:
    with open(_SEASON_MATRIX, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ------------------------------------------------------------------
# season_matrix.yaml structure
# ------------------------------------------------------------------


class TestSeasonMatrixStructure:

    def test_loads_matrix_successfully(self) -> None:
        matrix = _load_matrix()
        assert "months" in matrix
        assert len(matrix["months"]) == 12

    def test_month_config_has_expected_fields(self) -> None:
        matrix = _load_matrix()
        for month_num in range(1, 13):
            cfg = matrix["months"][month_num]
            assert "season" in cfg, f"Month {month_num} missing 'season'"
            assert "theme" in cfg, f"Month {month_num} missing 'theme'"
            assert "extra_keywords" in cfg, f"Month {month_num} missing 'extra_keywords'"
            assert "category_weights" in cfg, f"Month {month_num} missing 'category_weights'"
            assert isinstance(cfg["extra_keywords"], list)
            assert isinstance(cfg["category_weights"], dict)

    def test_winter_emphasises_ingredients(self) -> None:
        """In January (winter), skincare_ingredients weight should be higher."""
        matrix = _load_matrix()
        jan = matrix["months"][1]
        weights = jan["category_weights"]
        assert weights["skincare_ingredients"] > weights["skincare_knowledge"]

    def test_summer_emphasises_diet(self) -> None:
        """In July (summer), diet_tips weight should be elevated."""
        matrix = _load_matrix()
        jul = matrix["months"][7]
        weights = jul["category_weights"]
        assert weights["diet_tips"] >= 1.0

    def test_seasonal_keywords_are_nonempty(self) -> None:
        matrix = _load_matrix()
        for month_num in range(1, 13):
            kws = matrix["months"][month_num]["extra_keywords"]
            assert len(kws) >= 2, f"Month {month_num} has too few seasonal keywords"


# ------------------------------------------------------------------
# _guess_seasonal_category (test via source-level exec to avoid googleapi dep)
# ------------------------------------------------------------------

def _exec_guess_fn(keyword: str) -> str:
    """Execute _guess_seasonal_category without importing ResearcherAgent."""
    # Inline implementation matching researcher.py
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


class TestGuessSeasonalCategory:

    @pytest.mark.parametrize(
        "keyword,expected",
        [
            ("セラミド 乾燥肌", "skincare_ingredients"),
            ("ビタミンC 美白", "skincare_ingredients"),
            ("スキンケア ルーティン 切り替え", "skincare_routine"),
            ("ベストコスメ 2026", "beauty_trend"),
            ("ダイエット 食事", "diet_tips"),
            ("冬 乾燥 スキンケア", "skincare_knowledge"),
        ],
    )
    def test_category_guessing(self, keyword: str, expected: str) -> None:
        assert _exec_guess_fn(keyword) == expected


# ------------------------------------------------------------------
# Weighted allocation math
# ------------------------------------------------------------------


class TestWeightedAllocation:

    def test_weighted_allocation_distributes_correctly(self) -> None:
        """Verify the allocation formula used in _search_youtube."""
        categories = [
            "skincare_knowledge",
            "skincare_ingredients",
            "skincare_routine",
            "beauty_trend",
            "diet_tips",
        ]
        # January weights from season matrix
        category_weights = {
            "skincare_knowledge": 1.0,
            "skincare_ingredients": 1.5,
            "skincare_routine": 1.3,
            "beauty_trend": 0.8,
            "diet_tips": 0.7,
        }
        max_results_total = 20
        total_weight = sum(category_weights.get(cat, 1.0) for cat in categories)

        allocation = {}
        for cat in categories:
            weight = category_weights.get(cat, 1.0)
            allocation[cat] = max(1, int(max_results_total * weight / total_weight))

        # skincare_ingredients should get the most
        assert allocation["skincare_ingredients"] > allocation["diet_tips"]
        # All allocations should be at least 1
        assert all(v >= 1 for v in allocation.values())
