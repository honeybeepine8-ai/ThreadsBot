"""Review CLI for draft_queue: approve, reject, or edit pending posts.

Usage::

    python scripts/review.py              # interactive review of pending drafts
    python scripts/review.py list         # list all pending drafts (non-interactive)
    python scripts/review.py expire       # apply timeout rules to stale drafts
    python scripts/review.py stats        # show draft queue statistics
    python scripts/review.py notify       # send pending drafts to Telegram for review
    python scripts/review.py poll         # poll Telegram for approve/reject callbacks

Reject reason codes:
    weak_hook, off_topic, too_generic, bad_timing,
    similar_recent, compliance_risk, bot_feeling, factual_error
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile

import httpx
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.logger import get_logger
from core.notifier import Notifier
from core.state_manager import StateManager

logger = get_logger("review")

DRAFT_FILE = "draft_queue.json"
POST_QUEUE_FILE = "post_queue.json"

REJECT_REASON_CODES = [
    "weak_hook",
    "off_topic",
    "too_generic",
    "bad_timing",
    "similar_recent",
    "compliance_risk",
    "bot_feeling",
    "factual_error",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_drafts(sm: StateManager) -> dict[str, Any]:
    return sm.load_json(DRAFT_FILE)


def _save_drafts(sm: StateManager, data: dict[str, Any]) -> None:
    sm.save_json(DRAFT_FILE, data)


def _pending_drafts(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in data.get("drafts", []) if d.get("status") == "pending_review"]


def _format_draft(draft: dict[str, Any], index: int) -> str:
    """Pretty-print a single draft for the terminal."""
    lines = [
        f"\n{'='*60}",
        f"  [{index + 1}] ID: {draft['id']}  |  score: {draft.get('quality_score', '?')}  "
        f"|  pattern: {draft.get('pattern', '?')}",
        f"  category: {draft.get('category', '?')}  |  scheduled: {draft.get('scheduled_at', '?')}",
    ]
    flags = draft.get("flags", {})
    if flags.get("review_required"):
        lines.append("  *** REVIEW REQUIRED (Level 2 NG hit) ***")
    if flags.get("fact_check_required"):
        lines.append("  *** FACT CHECK REQUIRED ***")
    lines.append(f"  expires: {draft.get('expires_at', '?')}")
    lines.append(f"{'='*60}")
    lines.append("")

    # Content
    content = draft.get("content", "")
    lines.append(content)

    # Thread posts
    thread_posts = draft.get("thread_posts")
    if thread_posts:
        for i, tp in enumerate(thread_posts, 1):
            lines.append(f"\n  --- thread post {i} ---")
            lines.append(tp)

    # Affiliate comment
    aff = draft.get("affiliate_comment")
    if aff:
        lines.append(f"\n  [affiliate] {aff}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(sm: StateManager) -> None:
    """List all pending drafts."""
    data = _load_drafts(sm)
    pending = _pending_drafts(data)

    if not pending:
        print("\nレビュー待ちの下書きはありません。")
        return

    print(f"\nレビュー待ち: {len(pending)} 件")
    for i, draft in enumerate(pending):
        print(_format_draft(draft, i))


def cmd_stats(sm: StateManager) -> None:
    """Show draft queue statistics."""
    data = _load_drafts(sm)
    drafts = data.get("drafts", [])
    stats = data.get("stats", {})

    status_counts: dict[str, int] = {}
    for d in drafts:
        s = d.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    print("\n=== Draft Queue Stats ===")
    print(f"  Total drafts: {len(drafts)}")
    for status, count in sorted(status_counts.items()):
        print(f"    {status}: {count}")
    print()
    print("  Cumulative:")
    print(f"    generated:        {stats.get('total_generated', 0)}")
    print(f"    auto_approved:    {stats.get('auto_approved', 0)}")
    print(f"    manually_approved:{stats.get('manually_approved', 0)}")
    print(f"    rejected:         {stats.get('rejected', 0)}")
    print(f"    expired:          {stats.get('expired', 0)}")
    print()


def cmd_expire(sm: StateManager) -> None:
    """Apply timeout rules to stale drafts (12h unreviewd).

    - score >= 8.0 → auto-approve
    - score 7.5-7.9 → auto-discard
    """
    data = _load_drafts(sm)
    stats = data.setdefault("stats", {})
    # Batch: load post_queue once, accumulate, save once
    queue_data = sm.load_json(POST_QUEUE_FILE)
    existing_ids = {p["id"] for p in queue_data.get("queue", [])}

    approved_count = 0
    discarded_count = 0
    queue_changed = False

    for draft in _pending_drafts(data):
        expires_at_str = draft.get("expires_at")
        if not expires_at_str:
            continue

        try:
            expires_at = datetime.datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=datetime.timezone(datetime.timedelta(hours=9))
                )
            now_local = datetime.datetime.now(expires_at.tzinfo)
        except (ValueError, TypeError):
            continue

        if now_local < expires_at:
            continue

        score = draft.get("quality_score", 0.0)
        if score >= 8.0:
            draft["status"] = "approved"
            draft["review"] = {
                "action": "auto_approved_timeout",
                "reviewed_at": now_local.isoformat(),
            }
            post_item = _build_post_item_from_draft(draft)
            if post_item["id"] not in existing_ids:
                queue_data.setdefault("queue", []).append(post_item)
                existing_ids.add(post_item["id"])
                queue_changed = True
            stats["auto_approved"] = stats.get("auto_approved", 0) + 1
            approved_count += 1
            logger.info("Timeout auto-approved: %s (score=%.1f)", draft["id"], score)
        else:
            draft["status"] = "expired"
            draft["review"] = {
                "action": "auto_discarded_timeout",
                "reviewed_at": now_local.isoformat(),
            }
            stats["expired"] = stats.get("expired", 0) + 1
            discarded_count += 1
            logger.info("Timeout auto-discarded: %s (score=%.1f)", draft["id"], score)

    now_str = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ).isoformat()
    data["last_updated"] = now_str
    _save_drafts(sm, data)
    if queue_changed:
        queue_data["last_updated"] = now_str
        sm.save_json(POST_QUEUE_FILE, queue_data)

    print(f"\nExpire処理完了: {approved_count} 件承認, {discarded_count} 件破棄")


def cmd_review(sm: StateManager) -> None:
    """Interactive review session."""
    data = _load_drafts(sm)
    pending = _pending_drafts(data)

    if not pending:
        print("\nレビュー待ちの下書きはありません。")
        return

    print(f"\nレビュー待ち: {len(pending)} 件")
    print("操作: [a]pprove  [r]eject  [e]dit  [s]kip  [q]uit\n")

    now = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    )
    stats = data.setdefault("stats", {})
    reviewed_count = 0

    for i, draft in enumerate(pending):
        print(_format_draft(draft, i))

        while True:
            choice = input(f"  [{i+1}/{len(pending)}] 操作 (a/r/e/s/q): ").strip().lower()

            if choice in ("a", "approve"):
                draft["status"] = "approved"
                draft["review"] = {
                    "action": "approved",
                    "reviewed_at": now.isoformat(),
                }
                _move_to_post_queue(sm, draft, now)
                stats["manually_approved"] = stats.get("manually_approved", 0) + 1
                reviewed_count += 1
                print("  → 承認しました。post_queueに追加済み。\n")
                break

            elif choice in ("r", "reject"):
                reason = _ask_reject_reason()
                comment = input("  コメント (任意, Enter でスキップ): ").strip()
                draft["status"] = "rejected"
                draft["review"] = {
                    "action": "rejected",
                    "reason_code": reason,
                    "comment": comment or None,
                    "reviewed_at": now.isoformat(),
                }
                stats["rejected"] = stats.get("rejected", 0) + 1
                reviewed_count += 1
                print(f"  → 却下しました (reason: {reason})。\n")
                break

            elif choice in ("e", "edit"):
                edited_content = _edit_content(draft.get("content", ""))
                if edited_content and edited_content != draft.get("content"):
                    draft["original_content"] = draft["content"]
                    draft["content"] = edited_content
                    draft["status"] = "approved"
                    draft["review"] = {
                        "action": "edited",
                        "reviewed_at": now.isoformat(),
                    }
                    _move_to_post_queue(sm, draft, now)
                    stats["manually_approved"] = stats.get("manually_approved", 0) + 1
                    reviewed_count += 1
                    print("  → 編集して承認しました。\n")
                else:
                    print("  → 変更なし。もう一度選択してください。")
                    continue
                break

            elif choice in ("s", "skip"):
                print("  → スキップ。\n")
                break

            elif choice in ("q", "quit"):
                print(f"\n終了。{reviewed_count} 件レビュー済み。")
                data["last_updated"] = now.isoformat()
                _save_drafts(sm, data)
                return

            else:
                print("  無効な入力。a/r/e/s/q のいずれかを入力してください。")

    data["last_updated"] = now.isoformat()
    _save_drafts(sm, data)
    print(f"\n全件レビュー完了。{reviewed_count} 件処理済み。")


def cmd_notify(sm: StateManager) -> None:
    """Send all pending drafts to Telegram for review."""
    data = _load_drafts(sm)
    pending = _pending_drafts(data)

    if not pending:
        print("\nレビュー待ちの下書きはありません。")
        return

    notifier = Notifier()
    if not notifier.enabled:
        print("\nTelegram通知が設定されていません（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）。")
        return

    sent = 0
    for draft in pending:
        if notifier.send_draft_review(draft):
            sent += 1

    print(f"\nTelegramに {sent}/{len(pending)} 件の下書きを送信しました。")


def cmd_telegram_poll(sm: StateManager) -> None:
    """Poll Telegram for callback responses and apply approve/reject actions.

    This runs a one-shot poll: fetches recent callback queries from the
    Telegram Bot API and processes approve/reject actions.
    """
    notifier = Notifier()
    if not notifier.enabled:
        print("\nTelegram通知が設定されていません。")
        return

    # Get updates (callback queries)
    url = f"https://api.telegram.org/bot{notifier.bot_token}/getUpdates"
    poll_state = sm.load_json("telegram_poll_state.json")
    offset = poll_state.get("last_update_id", 0) + 1

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, params={"offset": offset, "timeout": 5})
        if resp.status_code != 200:
            print(f"Telegram API error: {resp.status_code}")
            return
        result = resp.json()
    except Exception as exc:
        print(f"Telegram API error: {exc}")
        return

    updates = result.get("result", [])
    if not updates:
        print("新しいコールバックはありません。")
        return

    data = _load_drafts(sm)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    stats = data.setdefault("stats", {})
    processed_count = 0
    last_update_id = poll_state.get("last_update_id", 0)

    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id > last_update_id:
            last_update_id = update_id

        callback = update.get("callback_query")
        if not callback:
            continue

        callback_data = callback.get("data", "")
        callback_id = callback.get("id", "")

        # Parse action:draft_id
        if ":" not in callback_data:
            continue

        action, draft_id = callback_data.split(":", 1)

        # Find draft
        draft = None
        for d in data.get("drafts", []):
            if d.get("id") == draft_id and d.get("status") == "pending_review":
                draft = d
                break

        # Answer callback to remove loading indicator
        _answer_callback(notifier.bot_token, callback_id, action, draft_id)

        if draft is None:
            continue

        if action == "approve":
            draft["status"] = "approved"
            draft["review"] = {
                "action": "approved",
                "reviewed_at": now.isoformat(),
                "reviewed_via": "telegram",
            }
            _move_to_post_queue(sm, draft, now)
            stats["manually_approved"] = stats.get("manually_approved", 0) + 1
            processed_count += 1
            logger.info("Telegram approved: %s", draft_id)

        elif action == "reject":
            draft["status"] = "rejected"
            draft["review"] = {
                "action": "rejected",
                "reason_code": "telegram_reject",
                "reviewed_at": now.isoformat(),
                "reviewed_via": "telegram",
            }
            stats["rejected"] = stats.get("rejected", 0) + 1
            processed_count += 1
            logger.info("Telegram rejected: %s", draft_id)

        # "later" → no action, leave as pending_review

    # Save state
    if processed_count > 0:
        data["last_updated"] = now.isoformat()
        _save_drafts(sm, data)

    poll_state["last_update_id"] = last_update_id
    sm.save_json("telegram_poll_state.json", poll_state)

    print(f"\n{processed_count} 件のレビューを処理しました（全 {len(updates)} 件のupdate）。")


def _answer_callback(bot_token: str, callback_id: str, action: str, draft_id: str) -> None:
    """Send answerCallbackQuery to Telegram to acknowledge the button press."""
    labels = {"approve": "✅ 承認しました", "reject": "❌ 却下しました", "later": "⏰ 保留"}
    text = labels.get(action, "処理済み")

    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    try:
        with httpx.Client(timeout=5) as client:
            client.post(url, json={"callback_query_id": callback_id, "text": text})
    except Exception:
        pass  # Best-effort


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_post_item_from_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Convert a draft dict into a post_queue-compatible item."""
    skip_keys = {"flags", "expires_at", "review", "original_content", "post_queue_id"}
    post_item: dict[str, Any] = {
        k: v for k, v in draft.items() if k not in skip_keys
    }
    post_item["id"] = draft.get("post_queue_id", draft["id"].replace("d_", "q_", 1))
    post_item["status"] = "pending"
    return post_item


def _move_to_post_queue(
    sm: StateManager,
    draft: dict[str, Any],
    now: datetime.datetime,
) -> None:
    """Move an approved draft into post_queue.json."""
    post_item = _build_post_item_from_draft(draft)

    queue_data = sm.load_json(POST_QUEUE_FILE)
    existing_ids = {p["id"] for p in queue_data.get("queue", [])}
    if post_item["id"] in existing_ids:
        logger.warning("Duplicate prevented: %s already in post_queue.", post_item["id"])
        return
    queue_data.setdefault("queue", []).append(post_item)
    queue_data["last_updated"] = now.isoformat()
    sm.save_json(POST_QUEUE_FILE, queue_data)


def _ask_reject_reason() -> str:
    """Prompt the user to select a reject reason code."""
    print("  却下理由:")
    for i, code in enumerate(REJECT_REASON_CODES, 1):
        print(f"    {i}. {code}")
    while True:
        val = input("  番号を選択: ").strip()
        try:
            idx = int(val) - 1
            if 0 <= idx < len(REJECT_REASON_CODES):
                return REJECT_REASON_CODES[idx]
        except ValueError:
            # Allow typing the code directly
            if val in REJECT_REASON_CODES:
                return val
        print("  無効な入力。もう一度。")


def _edit_content(content: str) -> str | None:
    """Open content in the user's $EDITOR for editing."""
    editor = os.environ.get("EDITOR", "notepad" if sys.platform == "win32" else "vi")
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(content)
            tmp_path = f.name

        subprocess.call([editor, tmp_path])

        with open(tmp_path, "r", encoding="utf-8") as f:
            edited = f.read().strip()

        os.unlink(tmp_path)
        return edited
    except Exception as exc:
        print(f"  エディタの起動に失敗: {exc}")
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    sm = StateManager()
    args = sys.argv[1:]
    command = args[0] if args else "review"

    commands = {
        "review": cmd_review,
        "list": cmd_list,
        "stats": cmd_stats,
        "expire": cmd_expire,
        "notify": cmd_notify,
        "poll": cmd_telegram_poll,
    }

    if command in ("--help", "-h"):
        print(__doc__)
        return

    handler = commands.get(command)
    if handler is None:
        print(f"Unknown command: {command}")
        print(f"Available: {', '.join(commands)}")
        sys.exit(1)

    handler(sm)


if __name__ == "__main__":
    main()
