"""CLI entry-point for running ThreadsBot agents.

Usage::

    python -m core.scheduler <agent_name>
    python -m core.scheduler poster       # run poster once
    python -m core.scheduler writer       # run writer once
    python -m core.scheduler all          # daemon: schedule all agents

When *all* is specified the process stays alive using APScheduler's
``BlockingScheduler``.  Schedule intervals are read from
``config/settings.yaml``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from core.account_context import AccountContext
from core.logger import get_logger

logger = get_logger("scheduler")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "settings.yaml"

# ---------------------------------------------------------------------------
# Agent registry — maps CLI names to (module_path, class_name) pairs.
# ---------------------------------------------------------------------------
_AGENT_REGISTRY: dict[str, tuple[str, str]] = {
    "researcher": ("agents.researcher", "ResearcherAgent"),
    "analyst": ("agents.analyst", "AnalystAgent"),
    "writer": ("agents.writer", "WriterAgent"),
    "poster": ("agents.poster", "PosterAgent"),
    "fetcher": ("agents.fetcher", "FetcherAgent"),
    "replier": ("agents.replier", "ReplierAgent"),
    "supervisor": ("agents.supervisor", "SupervisorAgent"),
}

HELP_TEXT = """\
ThreadsBot Scheduler
====================

Usage:
  python -m core.scheduler <agent_name>

Available agents:
  poster      Post next queued entry to Threads
  fetcher     Fetch metrics for recent posts
  replier     Reply to follower comments
  researcher  Gather research material
  analyst     Analyse post performance
  writer      Generate new post drafts
  supervisor  Monitor system health
  all         Start daemon that schedules all agents

Examples:
  python -m core.scheduler poster
  python -m core.scheduler all
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    """Load settings.yaml from the project config directory."""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _import_agent(name: str) -> Any:
    """Dynamically import and return the agent class for *name*."""
    module_path, class_name = _AGENT_REGISTRY[name]
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _run_single(name: str) -> None:
    """Instantiate and run a single agent by name."""
    ctx = AccountContext()
    ctx.load_env()

    try:
        agent_cls = _import_agent(name)
    except (ImportError, AttributeError) as exc:
        print(f"[ERROR] Could not import agent '{name}': {exc}", file=sys.stderr)
        sys.exit(1)

    logger.info("Running agent: %s", name)
    try:
        agent = agent_cls(ctx=ctx)
        agent.run()
    except Exception as exc:
        logger.error("Agent '%s' failed: %s", name, exc)
        print(f"[ERROR] Agent '{name}' failed: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Daemon (APScheduler)
# ---------------------------------------------------------------------------

def _build_scheduler_jobs(
    config: dict[str, Any],
) -> list[tuple[str, Callable[[], None], dict[str, Any]]]:
    """Return a list of ``(job_id, job_func, trigger_kwargs)`` tuples."""
    jobs: list[tuple[str, Callable[[], None], dict[str, Any]]] = []

    # Mapping: agent_name -> (config_section, interval_key)
    interval_agents: dict[str, tuple[str, str]] = {
        "researcher": ("researcher", "run_interval_hours"),
        "writer": ("poster", "run_interval_hours"),
        "poster": ("poster", "run_interval_hours"),
        "fetcher": ("fetcher", "run_interval_hours"),
        "replier": ("replier", "run_interval_minutes"),
        "supervisor": ("supervisor", "run_interval_minutes"),
    }

    for name in ("researcher", "poster", "fetcher", "writer", "replier", "supervisor"):
        try:
            agent_cls = _import_agent(name)
        except (ImportError, AttributeError) as exc:
            logger.warning("Skipping agent '%s' (not available): %s", name, exc)
            continue

        section, key = interval_agents[name]
        agent_config = config.get(section, {})

        if key.endswith("_minutes"):
            minutes = agent_config.get(key, 15)
            trigger_kwargs: dict[str, Any] = {"trigger": "interval", "minutes": minutes}
        elif key.endswith("_hours"):
            hours = agent_config.get(key, 1)
            trigger_kwargs = {"trigger": "interval", "hours": hours}
        else:
            trigger_kwargs = {"trigger": "interval", "hours": 1}

        def _make_job(cls: Any, agent_name: str) -> Callable[[], None]:
            def _job() -> None:
                logger.info("Scheduled run: %s", agent_name)
                ctx = AccountContext()
                ctx.load_env()
                try:
                    instance = cls(ctx=ctx)
                    instance.run()
                except Exception as exc:
                    logger.error("Scheduled agent '%s' failed: %s", agent_name, exc)
            return _job

        jobs.append((name, _make_job(agent_cls, name), trigger_kwargs))

    # Analyst uses a cron trigger (daily at a fixed time)
    try:
        analyst_cls = _import_agent("analyst")
        analyst_config = config.get("analyst", {})
        cron_time = analyst_config.get("run_cron", "05:00")
        hour_str, minute_str = cron_time.split(":")
        cron_kwargs: dict[str, Any] = {
            "trigger": "cron",
            "hour": int(hour_str),
            "minute": int(minute_str),
        }

        def _analyst_job(a_cls: Any = analyst_cls) -> None:
            logger.info("Scheduled run: analyst")
            ctx = AccountContext()
            ctx.load_env()
            try:
                instance = a_cls(ctx=ctx)
                instance.run()
            except Exception as exc:
                logger.error("Scheduled agent 'analyst' failed: %s", exc)

        jobs.append(("analyst", _analyst_job, cron_kwargs))
    except (ImportError, AttributeError) as exc:
        logger.warning("Skipping agent 'analyst' (not available): %s", exc)

    # Morning review: send email at configured time (cron), check reply on interval
    try:
        from core.morning_review import MorningReview

        mr_config = config.get("morning_review", {})
        send_time = mr_config.get("send_time", "07:00")
        send_hour, send_minute = send_time.split(":")
        check_interval = mr_config.get("check_interval_minutes", 10)

        def _morning_review_send() -> None:
            logger.info("Scheduled run: morning_review_send")
            ctx = AccountContext()
            ctx.load_env()
            try:
                MorningReview().send_morning_email()
            except Exception as exc:
                logger.error("morning_review_send failed: %s", exc)

        def _morning_review_check() -> None:
            ctx = AccountContext()
            ctx.load_env()
            try:
                MorningReview().check_approval_reply()
            except Exception as exc:
                logger.error("morning_review_check failed: %s", exc)

        jobs.append((
            "morning_review_send",
            _morning_review_send,
            {"trigger": "cron", "hour": int(send_hour), "minute": int(send_minute)},
        ))
        jobs.append((
            "morning_review_check",
            _morning_review_check,
            {"trigger": "interval", "minutes": check_interval},
        ))
        logger.info(
            "Morning review scheduled: send=%s, check_interval=%dmin",
            send_time, check_interval,
        )
    except ImportError as exc:
        logger.warning("Skipping morning_review jobs (not available): %s", exc)

    return jobs


def _run_daemon() -> None:
    """Start the blocking APScheduler daemon with all available agents."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    config = _load_config()
    all_jobs = _build_scheduler_jobs(config)

    if not all_jobs:
        print("[ERROR] No agents available to schedule.", file=sys.stderr)
        sys.exit(1)

    tz = config.get("app", {}).get("timezone", "Asia/Tokyo")
    scheduler = BlockingScheduler(timezone=tz)

    for job_id, func, trigger_kwargs in all_jobs:
        trigger = trigger_kwargs.pop("trigger")
        scheduler.add_job(func, trigger, id=job_id, **trigger_kwargs)
        logger.info("Scheduled '%s' with trigger '%s' %s", job_id, trigger, trigger_kwargs)

    print(f"ThreadsBot daemon started — {len(all_jobs)} job(s) scheduled.")
    print("Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Daemon stopped by user.")
        print("\nDaemon stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse CLI arguments and dispatch."""
    args = list(sys.argv[1:])

    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        sys.exit(0)

    agent_name = args[0].lower()

    if agent_name == "all":
        _run_daemon()
    elif agent_name in _AGENT_REGISTRY:
        _run_single(agent_name)
    else:
        print(f"[ERROR] Unknown agent: '{agent_name}'", file=sys.stderr)
        print(HELP_TEXT, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
