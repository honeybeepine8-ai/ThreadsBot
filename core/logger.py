"""Structured logging with JSON Lines format and automatic rotation.

V2 improvements:
- JSON Lines format for structured log queries
- TimedRotatingFileHandler with 30-day retention
- Console output remains human-readable (INFO+)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
_CONSOLE_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
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
        return json.dumps(entry, ensure_ascii=False)


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger with JSON Lines file and console handlers.

    Args:
        name: Logger name (e.g. ``"poster"``, ``"writer"``).

    Returns:
        A :class:`logging.Logger` instance with:
        - TimedRotatingFileHandler (DEBUG+, JSON Lines, midnight rotation, 30 backups)
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
    log_file = _LOG_DIR / "threadsbot.log"
    fh = logging.handlers.TimedRotatingFileHandler(
        str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    fh.suffix = "%Y-%m-%d"
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
