"""Structured logging with JSON Lines format and daily rotation.

V3 fix: Replace TimedRotatingFileHandler with _DateRotatingHandler to avoid
Windows PermissionError (WinError 32) when rotating a file held open by the
daemon process.  Logs are written directly to YYYY-MM-DD.log files; rotation
is a simple file-open, not a rename, so Windows file locking is a non-issue.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
_CONSOLE_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOG_RETENTION_DAYS = 30
_INITIALIZED_LOGGERS: set[str] = set()

_JST = timezone(timedelta(hours=9))


class _JsonFormatter(logging.Formatter):
    """Format log records as JSON Lines (one JSON object per line).

    Output example::

        {"ts":"2026-03-28T14:30:00+09:00","level":"INFO","agent":"poster","msg":"Post published"}
    """

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_JST)
        entry: dict[str, object] = {
            "ts": dt.isoformat(),
            "level": record.levelname,
            "agent": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["error"] = str(record.exc_info[1])
        import json
        return json.dumps(entry, ensure_ascii=False)


class _DateRotatingHandler(logging.FileHandler):
    """Write to YYYY-MM-DD.log; switch to a new file at midnight.

    Unlike ``TimedRotatingFileHandler``, rotation opens a *new* file instead
    of renaming the current one, avoiding the Windows ``PermissionError``
    (WinError 32) that occurs when another process holds the log file open.

    Args:
        log_dir: Directory where daily log files are created.
        backup_days: Days of logs to retain; older files are deleted on rotation.
    """

    def __init__(self, log_dir: Path, backup_days: int = _LOG_RETENTION_DAYS) -> None:
        self._log_dir = log_dir
        self._backup_days = backup_days
        self._current_date: date = datetime.now(tz=_JST).date()
        super().__init__(
            str(log_dir / f"{self._current_date}.log"),
            mode="a",
            encoding="utf-8",
        )

    def emit(self, record: logging.LogRecord) -> None:
        today: date = datetime.now(tz=_JST).date()
        if today != self._current_date:
            self._rotate(today)
        super().emit(record)

    def _rotate(self, new_date: date) -> None:
        """Switch to the new date's log file and prune old logs."""
        self.acquire()
        try:
            self._current_date = new_date
            self.baseFilename = str(self._log_dir / f"{new_date}.log")
            if self.stream:
                self.stream.flush()
                self.stream.close()
                self.stream = None
            self.stream = self._open()
            self._cleanup_old_logs()
        finally:
            self.release()

    def _cleanup_old_logs(self) -> None:
        """Delete YYYY-MM-DD.log files older than backup_days."""
        cutoff: date = datetime.now(tz=_JST).date() - timedelta(days=self._backup_days)
        for f in self._log_dir.glob("????-??-??.log"):
            try:
                if date.fromisoformat(f.stem) < cutoff:
                    f.unlink(missing_ok=True)
            except (ValueError, OSError):
                continue


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger with JSON Lines file and console handlers.

    Args:
        name: Logger name (e.g. ``"poster"``, ``"writer"``).

    Returns:
        A :class:`logging.Logger` instance with:
        - _DateRotatingHandler (DEBUG+, JSON Lines, daily YYYY-MM-DD.log files)
        - StreamHandler (INFO+, human-readable format)
    """
    if name in _INITIALIZED_LOGGERS:
        return logging.getLogger(name)

    _ensure_log_dir()

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers when get_logger is called multiple times
    if logger.handlers:
        _INITIALIZED_LOGGERS.add(name)
        return logger

    # --- File handler (DEBUG+, JSON Lines, daily rotation, 30-day retention) ---
    fh = _DateRotatingHandler(_LOG_DIR)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_JsonFormatter())
    logger.addHandler(fh)

    # --- Console handler (INFO+, human-readable) ---
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(ch)

    _INITIALIZED_LOGGERS.add(name)
    return logger
