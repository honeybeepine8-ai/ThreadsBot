"""Manually add a hook (first-line text) to knowledge/hook_stock.json.

Usage::

    python scripts/add_hook.py "セラミドって3種類あるの知ってた？"
    python scripts/add_hook.py "今使ってる化粧水、裏返してみて。" --category skincare_ingredients
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.constants import JST

_HOOK_STOCK_PATH = _PROJECT_ROOT / "knowledge" / "hook_stock.json"


def add_hook(text: str, category: str = "manual") -> None:
    """Add a hook entry to hook_stock.json."""
    if _HOOK_STOCK_PATH.exists():
        with open(_HOOK_STOCK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"last_updated": None, "hooks": []}

    hooks: list[dict] = data.setdefault("hooks", [])

    # Dedup check
    existing_texts = {h.get("text", "") for h in hooks}
    if text in existing_texts:
        print(f"Already exists: {text}")
        return

    now = datetime.now(JST)
    hooks.append({
        "text": text,
        "source": "manual",
        "category": category,
        "engagement_rate": 0.0,
        "added_at": now.isoformat(),
    })

    data["last_updated"] = now.isoformat()

    _HOOK_STOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(
        dir=str(_HOOK_STOCK_PATH.parent), suffix=".tmp"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(_HOOK_STOCK_PATH))

    print(f"Added: {text} (total: {len(hooks)})")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        return

    text = sys.argv[1]
    category = "manual"
    if "--category" in sys.argv:
        idx = sys.argv.index("--category")
        if idx + 1 < len(sys.argv):
            category = sys.argv[idx + 1]

    add_hook(text, category)


if __name__ == "__main__":
    main()
