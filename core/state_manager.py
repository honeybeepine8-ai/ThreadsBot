"""Atomic JSON state management for data/state/ directory."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Generator
from zoneinfo import ZoneInfo

from core.logger import get_logger

_STATE_DIR = Path(__file__).resolve().parent.parent / "data" / "state"

logger = get_logger("state_manager")

# Default schemas for each state file
_DEFAULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "post_history.json": {
        "last_updated": None,
        "posts": [],
        "daily_stats": {},
    },
    "post_queue.json": {
        "last_updated": None,
        "queue": [],
    },
    "research_pool.json": {
        "last_updated": None,
        "items": [],
    },
    "system_state.json": {
        "emergency_stop": False,
        "emergency_stop_reason": None,
        "emergency_stop_at": None,
        "last_health_check": None,
        "agent_status": {},
        "daily_counters": {},
    },
    "draft_queue.json": {
        "last_updated": None,
        "drafts": [],
        "stats": {
            "total_generated": 0,
            "auto_approved": 0,
            "manually_approved": 0,
            "rejected": 0,
            "expired": 0,
        },
    },
    "token_state.json": {
        "access_token": None,
        "refreshed_at": None,
        "expires_at": None,
    },
    "reply_state.json": {
        "last_updated": None,
        "processed_comments": {},
        "daily_reply_count": {},
    },
    "cross_post_state.json": {
        "last_updated": None,
        "cross_posts": [],
        "daily_counts": {},
    },
    "outbound_queue.json": {
        "last_updated": None,
        "targets": [],
        "daily_count": {},
    },
}


class StateManager:
    """Read and write JSON state files under ``data/state/``.

    All writes are atomic: data is first written to a temporary file in the
    same directory, then renamed to the target path.
    """

    def __init__(self, state_dir: Path | str | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else _STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_json(self, filename: str) -> dict[str, Any]:
        """Load a JSON file from the state directory.

        If the file does not exist, the matching default schema is returned
        (or an empty dict if no default is defined).

        Args:
            filename: Name of the JSON file (e.g. ``"post_history.json"``).

        Returns:
            Parsed JSON as a dictionary.
        """
        path = self.state_dir / filename
        if not path.exists():
            logger.debug("State file not found, returning default: %s", filename)
            return self._default_for(filename)

        try:
            with open(path, "r", encoding="utf-8") as f:
                data: dict[str, Any] = json.load(f)
            logger.debug("Loaded state file: %s", filename)
            return data
        except json.JSONDecodeError as exc:
            # Preserve corrupted file as .bak before resetting
            bak_name = f"{path.stem}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}"
            bak_path = path.parent / bak_name
            try:
                path.rename(bak_path)
                logger.error(
                    "JSON corrupted: %s — backed up to %s, resetting to default.",
                    filename, bak_name,
                )
            except OSError as rename_exc:
                logger.error(
                    "JSON corrupted: %s (%s) and backup failed: %s",
                    filename, exc, rename_exc,
                )
            return self._default_for(filename)
        except OSError as exc:
            logger.error("Failed to load %s: %s — returning default", filename, exc)
            return self._default_for(filename)

    def save_json(self, filename: str, data: dict[str, Any]) -> None:
        """Atomically write *data* to a JSON file in the state directory.

        The data is first written to a temporary file, then renamed so that
        readers never see a partially-written file.

        Args:
            filename: Target file name (e.g. ``"post_queue.json"``).
            data: Dictionary to serialise.
        """
        path = self.state_dir / filename
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.state_dir), suffix=".tmp", prefix=filename
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

            # On Windows, os.replace is atomic within the same volume.
            os.replace(tmp_path, str(path))
            logger.debug("Saved state file: %s", filename)
        except OSError as exc:
            logger.error("Failed to save %s: %s", filename, exc)
            # Clean up temp file on failure
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # ------------------------------------------------------------------
    # Locked update (read-modify-write with file lock)
    # ------------------------------------------------------------------
    #
    # Locking scope: APScheduler's BlockingScheduler runs agents
    # sequentially by default, so most read-modify-write cycles do not
    # need locking.  ``locked_update`` is used ONLY for counters in
    # ``system_state.json`` (daily post count, consecutive errors,
    # circuit breaker reset) where manual CLI runs could race with the
    # daemon.  Other state files (post_queue, post_history, etc.) are
    # protected by the scheduler's sequential execution guarantee.
    # ------------------------------------------------------------------

    def locked_update(
        self,
        filename: str,
        updater: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Atomically read, modify, and write a state file under a lock.

        This prevents TOCTOU race conditions when multiple processes or
        threads try to update the same file concurrently.

        Args:
            filename: State file name (e.g. ``"system_state.json"``).
            updater: A callable that receives the current data dict and
                mutates it in-place.

        Returns:
            The updated data dict after saving.
        """
        lock_path = self.state_dir / f".{filename}.lock"
        with self._file_lock(lock_path):
            data = self.load_json(filename)
            updater(data)
            self.save_json(filename, data)
            return data

    # Track which lock files the current thread holds to detect re-entrancy
    _held_locks: set[str] = set()

    @contextmanager
    def _file_lock(
        self, lock_path: Path, timeout: float = 10.0
    ) -> Generator[None, None, None]:
        """Simple cross-platform file lock using exclusive file creation.

        Args:
            lock_path: Path to the lock file.
            timeout: Max seconds to wait for the lock.

        Raises:
            TimeoutError: If the lock cannot be acquired within *timeout*.
            RuntimeError: If the same thread tries to acquire the lock twice
                (non-reentrant).
        """
        lock_key = str(lock_path)
        if lock_key in self._held_locks:
            raise RuntimeError(
                f"Deadlock prevented: _file_lock is not re-entrant ({lock_path.name})"
            )

        start = time.monotonic()
        fd = None
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() - start > timeout:
                    # Stale lock — force remove and retry once
                    try:
                        # Check if lock is stale (older than 60s)
                        age = time.time() - os.path.getmtime(str(lock_path))
                        if age > 60:
                            os.unlink(str(lock_path))
                            logger.warning("Removed stale lock: %s", lock_path.name)
                            continue
                    except OSError:
                        pass
                    raise TimeoutError(
                        f"Could not acquire lock on {lock_path.name} "
                        f"after {timeout}s"
                    )
                time.sleep(0.05)

        self._held_locks.add(lock_key)
        try:
            yield
        finally:
            self._held_locks.discard(lock_key)
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------

    ARCHIVE_THRESHOLD_DAYS = 90

    def archive_old_posts(self) -> int:
        """Move posts older than 90 days to monthly archive files.

        Returns:
            Number of posts archived.
        """
        jst = ZoneInfo("Asia/Tokyo")
        now = datetime.now(jst)
        threshold = now - timedelta(days=self.ARCHIVE_THRESHOLD_DAYS)

        history = self.load_json("post_history.json")
        posts: list[dict[str, Any]] = history.get("posts", [])

        keep: list[dict[str, Any]] = []
        archive_buckets: dict[str, list[dict[str, Any]]] = {}

        for post in posts:
            posted_at_str = post.get("posted_at")
            if not posted_at_str:
                keep.append(post)
                continue
            try:
                posted_at = datetime.fromisoformat(posted_at_str)
                # Ensure both sides are tz-aware to avoid TypeError
                if posted_at.tzinfo is None and threshold.tzinfo is not None:
                    posted_at = posted_at.replace(tzinfo=threshold.tzinfo)
            except (ValueError, TypeError):
                keep.append(post)
                continue

            if posted_at >= threshold:
                keep.append(post)
            else:
                month_key = posted_at.strftime("%Y-%m")
                archive_buckets.setdefault(month_key, []).append(post)

        if not archive_buckets:
            logger.debug("No posts to archive (all within %d days).", self.ARCHIVE_THRESHOLD_DAYS)
            return 0

        archive_dir = self.state_dir.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        total_archived = 0
        for month, archived_posts in archive_buckets.items():
            archive_path = archive_dir / f"post_history_{month}.json"

            existing: list[dict[str, Any]] = []
            if archive_path.exists():
                try:
                    with open(archive_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    existing = data.get("posts", [])
                except (json.JSONDecodeError, OSError):
                    existing = []

            existing.extend(archived_posts)

            archive_data = {
                "month": month,
                "last_updated": now.isoformat(),
                "posts": existing,
            }

            fd, tmp_path = tempfile.mkstemp(
                dir=str(archive_dir), suffix=".tmp", prefix=f"post_history_{month}"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(archive_data, f, ensure_ascii=False, indent=2, default=str)
                os.replace(tmp_path, str(archive_path))
            except OSError:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            total_archived += len(archived_posts)
            logger.info("Archived %d posts to %s.", len(archived_posts), archive_path.name)

        history["posts"] = keep
        self.save_json("post_history.json", history)
        logger.info("Archive complete: %d posts moved, %d kept.", total_archived, len(keep))
        return total_archived

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_for(filename: str) -> dict[str, Any]:
        """Return a deep copy of the default schema for *filename*."""
        import copy

        schema = _DEFAULT_SCHEMAS.get(filename)
        if schema is None:
            return {}
        return copy.deepcopy(schema)
