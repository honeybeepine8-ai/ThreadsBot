"""Backup script — zip data/state/ and rotate old backups.

Usage::

    python scripts/backup.py              # run backup now
    python scripts/backup.py --dry-run    # show what would be backed up

Designed to run daily via Task Scheduler or cron (e.g. 04:00 JST).
Keeps up to 30 generations of backups.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.logger import get_logger

logger = get_logger("backup")

STATE_DIR = _PROJECT_ROOT / "data" / "state"
BACKUP_DIR = _PROJECT_ROOT / "data" / "backups"
MAX_GENERATIONS = 30


def run_backup(dry_run: bool = False) -> Path | None:
    """Create a zip backup of data/state/ and rotate old backups.

    Args:
        dry_run: If True, only print what would happen without acting.

    Returns:
        Path to the created zip file, or None on dry-run / error.
    """
    if not STATE_DIR.exists():
        logger.warning("State directory does not exist: %s", STATE_DIR)
        print(f"State directory not found: {STATE_DIR}")
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"state_{timestamp}"
    zip_path = BACKUP_DIR / f"{zip_name}.zip"

    # Count files to back up
    state_files = list(STATE_DIR.glob("*.json"))
    total_size = sum(f.stat().st_size for f in state_files if f.exists())

    if dry_run:
        print(f"[DRY RUN] Would backup {len(state_files)} files ({total_size:,} bytes)")
        print(f"[DRY RUN] Target: {zip_path}")
        _show_rotation_plan()
        return None

    # 1. Create zip archive
    try:
        archive_base = str(zip_path.with_suffix(""))
        shutil.make_archive(archive_base, "zip", str(STATE_DIR))
        logger.info(
            "Backup created: %s (%d files, %s bytes)",
            zip_path.name,
            len(state_files),
            f"{total_size:,}",
        )
    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        print(f"Backup failed: {exc}")
        return None

    # 2. Rotate old backups (keep MAX_GENERATIONS)
    rotated = _rotate_backups()
    if rotated:
        logger.info("Rotated %d old backup(s).", rotated)

    print(f"Backup completed: {zip_path.name}")
    return zip_path


def _rotate_backups() -> int:
    """Delete backups exceeding MAX_GENERATIONS, keeping the newest."""
    backups = sorted(BACKUP_DIR.glob("state_*.zip"))
    to_delete = backups[:-MAX_GENERATIONS] if len(backups) > MAX_GENERATIONS else []

    for old in to_delete:
        try:
            old.unlink()
            logger.debug("Deleted old backup: %s", old.name)
        except OSError as exc:
            logger.warning("Failed to delete old backup %s: %s", old.name, exc)

    return len(to_delete)


def _show_rotation_plan() -> None:
    """Print rotation info for dry-run."""
    backups = sorted(BACKUP_DIR.glob("state_*.zip"))
    excess = len(backups) - MAX_GENERATIONS + 1  # +1 for the new one
    if excess > 0:
        print(f"[DRY RUN] Would delete {excess} old backup(s)")
    else:
        print(f"[DRY RUN] No rotation needed ({len(backups)}/{MAX_GENERATIONS} slots used)")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    run_backup(dry_run=dry_run)


if __name__ == "__main__":
    main()
