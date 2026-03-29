"""Add outbound reply targets to the queue.

Usage::

    python scripts/add_outbound.py <media_id> "投稿の内容・文脈"
    python scripts/add_outbound.py list           # キュー一覧
    python scripts/add_outbound.py clear           # 完了済みを削除
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.constants import JST
from core.state_manager import StateManager


def add_target(sm: StateManager, media_id: str, context: str) -> None:
    """Add a new outbound target to the queue."""
    now = datetime.datetime.now(JST)
    queue = sm.load_json("outbound_queue.json")
    targets: list[dict] = queue.setdefault("targets", [])

    # Check for duplicate
    existing_ids = {t.get("target_media_id") for t in targets}
    if media_id in existing_ids:
        print(f"Already in queue: {media_id}")
        return

    targets.append({
        "target_media_id": media_id,
        "context": context,
        "status": "pending",
        "added_at": now.isoformat(),
    })
    queue["last_updated"] = now.isoformat()
    sm.save_json("outbound_queue.json", queue)
    print(f"Added: {media_id} ({context[:50]}...)")


def list_targets(sm: StateManager) -> None:
    """Display all targets in the queue."""
    queue = sm.load_json("outbound_queue.json")
    targets = queue.get("targets", [])

    if not targets:
        print("Queue is empty.")
        return

    for i, t in enumerate(targets, 1):
        status = t.get("status", "?")
        media_id = t.get("target_media_id", "?")
        context = t.get("context", "")[:60]
        print(f"  {i}. [{status:10s}] {media_id} — {context}")

    pending = sum(1 for t in targets if t.get("status") == "pending")
    posted = sum(1 for t in targets if t.get("status") == "posted")
    print(f"\n  Total: {len(targets)} (pending: {pending}, posted: {posted})")


def clear_completed(sm: StateManager) -> None:
    """Remove completed/error targets from the queue."""
    queue = sm.load_json("outbound_queue.json")
    targets = queue.get("targets", [])
    before = len(targets)
    queue["targets"] = [t for t in targets if t.get("status") == "pending"]
    after = len(queue["targets"])
    sm.save_json("outbound_queue.json", queue)
    print(f"Removed {before - after} completed targets ({after} remaining).")


def main() -> None:
    sm = StateManager()

    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "list":
        list_targets(sm)
    elif cmd == "clear":
        clear_completed(sm)
    elif len(sys.argv) >= 3:
        add_target(sm, media_id=cmd, context=" ".join(sys.argv[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
