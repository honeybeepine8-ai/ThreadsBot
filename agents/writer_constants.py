"""Writer-shared constants and file-loading utilities.

Extracted from ``writer.py`` to keep the main agent module under 500 lines
while giving mixins a clean, non-circular import target.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Category → hashtag mapping
_CATEGORY_HASHTAGS: dict[str, str] = {
    "skincare_knowledge": "#スキンケア",
    "skincare_ingredients": "#美容成分",
    "skincare_routine": "#スキンケアルーティン",
    "beauty_trend": "#美容トレンド",
    "diet_tips": "#ダイエット",
}

# Quality scoring: 3-group averaging (V3.1 — 10 items)
_CONTENT_QUALITY_GROUP: frozenset[str] = frozenset(
    {"usefulness", "specificity", "empathy", "call_to_action"}
)
_EXPRESSION_QUALITY_GROUP: frozenset[str] = frozenset(
    {"naturalness", "tempo", "experiential", "non_commercial"}
)
_HOOK_QUALITY_GROUP: frozenset[str] = frozenset(
    {"hook_strength", "persona_match"}
)

# All posting patterns (21 total as of V3.1)
_ALL_PATTERNS: list[str] = [
    "短文完結型",
    "コメント誘導型",
    "ツリー展開型",
    "暴露・裏話系",
    "需要確認型",
    "リスト系",
    "ビフォーアフター型",
    "反常識型",
    "実体験レビュー型",
    "タイムライン型",
    "二択・比較型",
    "あるある共感型",
    "数字インパクト型",
    "ストーリー型",
    "まとめ・結論先出型",
    "失敗談型",          # V3.1追加
    "ランキング異議型",  # V3.1追加
    "季節タイムリー型",  # V3.1追加
    "UGCテンプレ型",
    "質問募集型",
    "フォロワー質問回答型",
]

# Pattern classification for distribution control (V3.1)
_KAKUSAN_PATTERNS: frozenset[str] = frozenset({
    "コメント誘導型",
    "反常識型",
    "短文完結型",
    "二択・比較型",
    "あるある共感型",
    "暴露・裏話系",
    "需要確認型",
    "失敗談型",
    "ランキング異議型",
    "実体験レビュー型",
})
_HOZON_PATTERNS: frozenset[str] = frozenset({
    "リスト系",
    "まとめ・結論先出型",
    "タイムライン型",
    "数字インパクト型",
    "ビフォーアフター型",
    "ストーリー型",
})
_BOTH_PATTERNS: frozenset[str] = frozenset({
    "季節タイムリー型",
})
# Special-purpose patterns excluded from normal distribution ratios
_SPECIAL_PATTERNS: frozenset[str] = frozenset({
    "ツリー展開型",
    "UGCテンプレ型",
    "質問募集型",
    "フォロワー質問回答型",
})

# Day-of-week name mapping for schedule.yaml keys
_WEEKDAY_NAMES: list[str] = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

# Profile CTA templates (appended to post body to drive profile visits)
_PROFILE_CTA_TEMPLATES: list[str] = [
    "\n\nプロフにおすすめまとめてるよ→",
    "\n\n気になる人はプロフ見てみてね",
    "\n\nもっと知りたい人→プロフにリンクあるよ",
    "\n\n詳しくはプロフにまとめてるよ",
]


# ------------------------------------------------------------------
# File I/O helpers
# ------------------------------------------------------------------

def load_yaml(relative_path: str) -> dict[str, Any]:
    """Load a YAML file relative to the project root."""
    full_path = _PROJECT_ROOT / relative_path
    with open(full_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_text(relative_path: str) -> str:
    """Load a text file relative to the project root."""
    full_path = _PROJECT_ROOT / relative_path
    with open(full_path, encoding="utf-8") as f:
        return f.read()


def load_audience_data() -> dict[str, Any]:
    """Load audience.json (TOP5/BOTTOM3 + feedback) if it exists."""
    path = _PROJECT_ROOT / "data" / "analytics" / "audience.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            pass
    return {}
