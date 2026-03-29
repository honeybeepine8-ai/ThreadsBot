"""Initialize ThreadsBot data files with default schemas.

Usage::

    python scripts/init_data.py           # create missing files only
    python scripts/init_data.py --force   # overwrite existing files

Creates the following JSON files with their default content:

- data/state/post_history.json
- data/state/post_queue.json
- data/state/draft_queue.json
- data/state/research_pool.json
- data/state/system_state.json
- data/analytics/performance.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so core.* imports work.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_DATA_DIR = _PROJECT_ROOT / "data"

# ---------------------------------------------------------------------------
# Default schemas — reuse from state_manager (single source of truth)
# ---------------------------------------------------------------------------
from core.state_manager import _DEFAULT_SCHEMAS as _STATE_SCHEMAS  # noqa: E402

import copy

_FILE_DEFAULTS: dict[str, dict[str, Any]] = {
    f"state/{name}": copy.deepcopy(schema)
    for name, schema in _STATE_SCHEMAS.items()
}
# Add analytics files (not managed by state_manager)
_FILE_DEFAULTS["analytics/performance.json"] = {
    "last_updated": None,
    "daily_reports": [],
    "trend_summary": {},
}


def init_data_files(*, force: bool = False) -> list[str]:
    """Create data files with default content.

    Args:
        force: If ``True``, overwrite existing files. Otherwise skip them.

    Returns:
        List of file paths (relative to ``data/``) that were created or
        overwritten.
    """
    created: list[str] = []

    for relative_path, default_content in _FILE_DEFAULTS.items():
        target = _DATA_DIR / relative_path

        if target.exists() and not force:
            print(f"  [SKIP] {relative_path} (already exists)")
            continue

        # Ensure parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        with open(target, "w", encoding="utf-8") as f:
            json.dump(default_content, f, ensure_ascii=False, indent=2)

        action = "OVERWRITE" if target.exists() and force else "CREATE"
        print(f"  [{action}] {relative_path}")
        created.append(relative_path)

    return created


def main() -> None:
    """Parse arguments and run initialization."""
    force = "--force" in sys.argv

    print("ThreadsBot Data Initializer")
    print("===========================")
    if force:
        print("Mode: FORCE (overwriting existing files)")
    else:
        print("Mode: safe (skipping existing files)")
    print()

    created = init_data_files(force=force)

    print()
    if created:
        print(f"Done - {len(created)} file(s) created.")
    else:
        print("Done - all files already exist. Use --force to overwrite.")


if __name__ == "__main__":
    main()
