"""Generate structured review cards for pending drafts.

Usage::

    python scripts/review_display.py          # JSON output for all pending
    python scripts/review_display.py <id>     # single draft by ID

Output is UTF-8 JSON to stdout, designed for consumption by Claude Code
or other review tools.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.constants import JST
from core.state_manager import StateManager

# Pattern descriptions for context
_PATTERN_DESCRIPTIONS: dict[str, str] = {
    "短文完結型": "短くインパクトのある一言系",
    "コメント誘導型": "読者に問いかけてコメントを促す",
    "ツリー展開型": "スレッド形式で深掘り",
    "暴露・裏話系": "業界の裏側・意外な事実",
    "需要確認型": "読者の反応を探る",
    "リスト系": "箇条書きで情報をまとめる",
    "ビフォーアフター型": "比較で効果を見せる",
    "反常識型": "常識を覆す切り口",
    "実体験レビュー型": "体験ベースの語り",
    "タイムライン型": "時系列で変化を見せる",
    "二択・比較型": "2つの選択肢を比較",
    "あるある共感型": "あるあるネタで共感を得る",
    "数字インパクト型": "数字で驚きを与える",
    "ストーリー型": "物語形式で引き込む",
    "まとめ・結論先出型": "結論から入る情報整理",
    "UGCテンプレ型": "フォロワー参加型テンプレ",
    "質問募集型": "フォロワーからの質問を募集",
    "フォロワー質問回答型": "寄せられた質問に回答",
}

# Category labels
_CATEGORY_LABELS: dict[str, str] = {
    "skincare_knowledge": "スキンケア知識",
    "skincare_ingredients": "美容成分",
    "skincare_routine": "スキンケアルーティン",
    "beauty_trend": "美容トレンド",
    "diet_tips": "ダイエット",
}


def _load_research_pool(sm: StateManager) -> dict[str, dict[str, Any]]:
    """Load research pool indexed by ID."""
    data = sm.load_json("research_pool.json")
    return {item["id"]: item for item in data.get("items", [])}


def _load_post_history(sm: StateManager) -> list[dict[str, Any]]:
    """Load recent post history."""
    data = sm.load_json("post_history.json")
    return data.get("posts", [])


def _build_review_card(
    draft: dict[str, Any],
    research_pool: dict[str, dict[str, Any]],
    post_history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a structured review card for a single draft."""
    now = datetime.datetime.now(JST)

    # Research source
    research_id = draft.get("research_id", "")
    research = research_pool.get(research_id, {})

    # Time until expiry
    expires_at_str = draft.get("expires_at", "")
    remaining = ""
    if expires_at_str:
        try:
            expires_at = datetime.datetime.fromisoformat(expires_at_str)
            delta = expires_at - now
            if delta.total_seconds() > 0:
                hours = int(delta.total_seconds() // 3600)
                minutes = int((delta.total_seconds() % 3600) // 60)
                remaining = f"{hours}h{minutes}m"
            else:
                remaining = "期限切れ"
        except (ValueError, TypeError):
            remaining = "不明"

    # Scheduled time display
    scheduled_at = draft.get("scheduled_at", "")
    schedule_display = ""
    if scheduled_at:
        try:
            st = datetime.datetime.fromisoformat(scheduled_at)
            schedule_display = st.strftime("%m/%d %H:%M")
        except (ValueError, TypeError):
            schedule_display = scheduled_at

    # Safety flags summary
    flags: list[str] = []
    if draft.get("pharma_law_check") == "borderline":
        flags.append("薬機法ボーダーライン")
    if draft.get("fact_check_required"):
        flags.append("ファクトチェック要")
    if draft.get("ng_review_required"):
        flags.append("NGワード要確認")
    if draft.get("profile_cta_included"):
        flags.append("プロフCTA付き")
    if draft.get("cta_pr_label"):
        flags.append("PR表記あり")
    if draft.get("debate_id"):
        flags.append(f"論争テーマ: {draft.get('debate_title', '?')}")
    unknown_claims = draft.get("fact_check_unknown_claims", [])
    if unknown_claims:
        flags.append(f"未検証の数値: {', '.join(unknown_claims)}")

    if not flags:
        flags.append("安全チェックすべてOK")

    # Pattern info
    pattern = draft.get("pattern", "不明")
    pattern_desc = _PATTERN_DESCRIPTIONS.get(pattern, "")

    # Category
    category = draft.get("category", "")
    category_label = _CATEGORY_LABELS.get(category, category)

    # Similarity context
    similarity = draft.get("similarity_score", 0.0)
    if similarity < 0.3:
        similarity_note = "十分にユニーク"
    elif similarity < 0.5:
        similarity_note = "やや類似あり"
    elif similarity < 0.7:
        similarity_note = "類似度高め（要注意）"
    else:
        similarity_note = "非常に類似（要確認）"

    # Recent posts with same category (for context)
    same_category_recent = [
        p for p in post_history[-20:]
        if p.get("category") == category
    ]

    # Thread posts
    thread_posts = draft.get("thread_posts")

    # Build card
    card: dict[str, Any] = {
        "id": draft["id"],
        "post_queue_id": draft.get("post_queue_id", ""),
        "content": draft.get("content", ""),
        "thread_posts": thread_posts,
        "char_count": len(draft.get("content", "")),
        "quality_score": draft.get("quality_score", 0),
        "pattern": pattern,
        "pattern_description": pattern_desc,
        "category": category_label,
        "hashtag": draft.get("hashtag", ""),
        "scheduled_at": schedule_display,
        "expires_remaining": remaining,
        "similarity": {
            "score": round(similarity, 2),
            "note": similarity_note,
        },
        "safety_flags": flags,
        "research_source": {
            "topic": research.get("topic", "不明"),
            "summary": research.get("summary", ""),
            "source": research.get("source", ""),
            "source_url": research.get("source_url", ""),
            "keywords": research.get("keywords", []),
        },
        "same_category_recent_count": len(same_category_recent),
        "recommendation": _generate_recommendation(draft, same_category_recent),
    }

    return card


def _generate_recommendation(
    draft: dict[str, Any],
    same_category_recent: list[dict[str, Any]],
) -> dict[str, str]:
    """Generate a recommendation for the draft."""
    score = draft.get("quality_score", 0)
    reasons: list[str] = []
    verdict = "approve"

    # Score-based
    if score >= 8.0:
        reasons.append(f"品質スコア {score} — 高品質")
    elif score >= 7.5:
        reasons.append(f"品質スコア {score} — 基準クリア")
    else:
        reasons.append(f"品質スコア {score} — 基準ギリギリ")
        verdict = "review_carefully"

    # Safety
    if draft.get("pharma_law_check") == "borderline":
        reasons.append("薬機法ボーダーライン — 表現を確認してください")
        verdict = "review_carefully"
    if draft.get("fact_check_required"):
        reasons.append("未検証の事実があります")
        verdict = "review_carefully"

    # Category saturation
    if len(same_category_recent) >= 3:
        reasons.append(f"同カテゴリが直近20件中{len(same_category_recent)}件 — テーマが偏り気味")

    # Similarity
    sim = draft.get("similarity_score", 0)
    if sim > 0.5:
        reasons.append(f"類似度 {sim:.0%} — 過去投稿と似ている可能性")
        verdict = "review_carefully"

    # Pattern
    if draft.get("pattern") == "コメント誘導型":
        reasons.append("コメント誘導型 — エンゲージメント獲得に効果的")
    if draft.get("pattern") == "ビフォーアフター型":
        reasons.append("ビフォーアフター型 — 視覚的な比較で説得力あり")

    return {
        "verdict": verdict,
        "reasons": reasons,
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sm = StateManager()
    draft_data = sm.load_json("draft_queue.json")
    drafts = draft_data.get("drafts", [])

    # Filter target
    target_id = sys.argv[1] if len(sys.argv) > 1 else None
    if target_id:
        drafts = [d for d in drafts if d["id"] == target_id]
    else:
        drafts = [d for d in drafts if d.get("status") == "pending_review"]

    if not drafts:
        print(json.dumps({"pending_count": 0, "cards": []}, ensure_ascii=False))
        return

    research_pool = _load_research_pool(sm)
    post_history = _load_post_history(sm)

    cards = [
        _build_review_card(d, research_pool, post_history)
        for d in drafts
    ]

    output = {
        "pending_count": len(cards),
        "generated_at": datetime.datetime.now(JST).isoformat(),
        "cards": cards,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
