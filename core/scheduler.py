"""CLI entry-point for running ThreadsBot agents.

Usage::

    python -m core.scheduler <agent_name>
    python -m core.scheduler poster                        # run poster once (default account)
    python -m core.scheduler writer                        # run writer once
    python -m core.scheduler all                           # daemon: schedule all agents
    python -m core.scheduler --account riku writer         # run writer for account 'riku'
    python -m core.scheduler --account all all             # daemon: all agents × all accounts

When *all* is specified the process stays alive using APScheduler's
``BlockingScheduler``.  Schedule intervals are read from
``config/settings.yaml``.  Agents that have not been implemented yet are
silently skipped (``ImportError`` is caught).
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
    "cross_poster": ("agents.cross_poster", "CrossPosterAgent"),
}

HELP_TEXT = """\
ThreadsBot Scheduler
====================

Usage:
  python -m core.scheduler [--account <id>] <agent_name>

Options:
  --account <id>   Account to run (default: "default" = project root).
                   Use "all" to run for every registered account.

Available agents:
  poster      Post next queued entry to Threads
  fetcher     Fetch metrics for recent posts
  replier     Reply to follower comments
  researcher  Gather research material
  analyst     Analyse post performance
  writer      Generate new post drafts
  supervisor     Monitor system health
  cross_poster   Cross-account quote reposts
  all            Start daemon that schedules all agents

Examples:
  python -m core.scheduler poster
  python -m core.scheduler --account riku writer
  python -m core.scheduler --account all all
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(ctx: AccountContext | None = None) -> dict[str, Any]:
    """Load settings.yaml via AccountContext (merged) or global fallback."""
    if ctx is not None:
        return ctx.load_settings()
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _import_agent(name: str) -> Any:
    """Dynamically import and return the agent class for *name*.

    Raises ``ImportError`` if the module or class is not available.
    """
    module_path, class_name = _AGENT_REGISTRY[name]
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _list_accounts() -> list[str]:
    """Return registered account IDs (excluding _template)."""
    accounts_dir = _PROJECT_ROOT / "accounts"
    if not accounts_dir.exists():
        return []
    return sorted(
        d.name
        for d in accounts_dir.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )


def _run_single(name: str, ctx: AccountContext | None = None) -> None:
    """Instantiate and run a single agent by name."""
    ctx = ctx or AccountContext("default")
    ctx.load_env()

    try:
        agent_cls = _import_agent(name)
    except (ImportError, AttributeError) as exc:
        print(f"[ERROR] Could not import agent '{name}': {exc}", file=sys.stderr)
        sys.exit(1)

    label = f"{name}@{ctx.account_id}"
    logger.info("Running agent: %s", label)
    try:
        agent = agent_cls(ctx=ctx)
        agent.run()
    except Exception as exc:
        logger.error("Agent '%s' failed: %s", label, exc)
        print(f"[ERROR] Agent '{label}' failed: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Daemon (APScheduler)
# ---------------------------------------------------------------------------

def _build_scheduler_jobs(
    config: dict[str, Any],
    ctx: AccountContext | None = None,
) -> list[tuple[str, Callable[[], None], dict[str, Any]]]:
    """Return a list of ``(job_id, job_func, trigger_kwargs)`` tuples.

    Agents that cannot be imported are excluded with a warning.
    """
    ctx = ctx or AccountContext("default")
    suffix = f"@{ctx.account_id}" if ctx.account_id != "default" else ""
    jobs: list[tuple[str, Callable[[], None], dict[str, Any]]] = []

    # Mapping: agent_name -> (config_section, interval_key)
    interval_agents: dict[str, tuple[str, str]] = {
        "researcher": ("researcher", "run_interval_hours"),
        "writer": ("writer", "queue_target_size"),  # writer runs on poster interval
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

        # Build trigger kwargs based on unit
        if key.endswith("_minutes"):
            minutes = agent_config.get(key, 15)
            trigger_kwargs: dict[str, Any] = {"trigger": "interval", "minutes": minutes}
        elif key.endswith("_hours"):
            hours = agent_config.get(key, 1)
            trigger_kwargs = {"trigger": "interval", "hours": hours}
        else:
            # Fallback: 1-hour interval
            trigger_kwargs = {"trigger": "interval", "hours": 1}

        def _make_job(cls: Any, agent_name: str, agent_ctx: AccountContext) -> Callable[[], None]:
            """Create a closure that runs the agent with error handling."""
            def _job() -> None:
                label = f"{agent_name}{suffix}"
                logger.info("Scheduled run: %s", label)
                agent_ctx.load_env()
                try:
                    instance = cls(ctx=agent_ctx)
                    instance.run()
                except Exception as exc:
                    logger.error("Scheduled agent '%s' failed: %s", label, exc)
            return _job

        job_id = f"{name}{suffix}"
        jobs.append((job_id, _make_job(agent_cls, name, ctx), trigger_kwargs))

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

        def _analyst_job(a_cls: Any = analyst_cls, a_ctx: AccountContext = ctx) -> None:
            label = f"analyst{suffix}"
            logger.info("Scheduled run: %s", label)
            a_ctx.load_env()
            try:
                instance = a_cls(ctx=a_ctx)
                instance.run()
            except Exception as exc:
                logger.error("Scheduled agent '%s' failed: %s", label, exc)

        jobs.append((f"analyst{suffix}", _analyst_job, cron_kwargs))
    except (ImportError, AttributeError) as exc:
        logger.warning("Skipping agent 'analyst' (not available): %s", exc)

    return jobs


def _run_daemon(ctx: AccountContext | None = None) -> None:
    """Start the blocking APScheduler daemon with all available agents.

    When *ctx* is ``None`` and ``--account all`` was specified, jobs for
    every registered account are scheduled with a per-account time offset
    to avoid API contention.
    """
    import time as _time
    from apscheduler.schedulers.blocking import BlockingScheduler

    all_jobs: list[tuple[str, Callable[[], None], dict[str, Any]]] = []

    # Per-account jitter offset (seconds) to avoid simultaneous API hits.
    _ACCOUNT_JITTER_SECONDS = 60

    if ctx is None:
        # --account all: schedule jobs for every account
        accounts = _list_accounts()
        if not accounts:
            print("[ERROR] No accounts found in accounts/ directory.", file=sys.stderr)
            sys.exit(1)
        for idx, acct_id in enumerate(accounts):
            acct_ctx = AccountContext(acct_id)
            config = _load_config(acct_ctx)
            jobs = _build_scheduler_jobs(config, acct_ctx)
            # Stagger each account's jobs by a fixed offset
            jitter = idx * _ACCOUNT_JITTER_SECONDS
            if jitter > 0:
                for job_id, func, trigger_kwargs in jobs:
                    trigger_kwargs["jitter"] = jitter
            all_jobs.extend(jobs)
    else:
        config = _load_config(ctx)
        all_jobs = _build_scheduler_jobs(config, ctx)

    if not all_jobs:
        print("[ERROR] No agents available to schedule.", file=sys.stderr)
        sys.exit(1)

    global_config = _load_config(AccountContext("default"))
    tz = global_config.get("app", {}).get("timezone", "Asia/Tokyo")
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

def _parse_args() -> tuple[str, str]:
    """Parse CLI args and return ``(account_id, agent_name)``.

    ``account_id`` is ``"default"`` when ``--account`` is not specified.
    """
    args = list(sys.argv[1:])

    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        sys.exit(0)

    account_id = "default"
    if args[0] == "--account":
        if len(args) < 3:
            print("[ERROR] --account requires <id> and <agent_name>", file=sys.stderr)
            sys.exit(1)
        account_id = args[1].lower()
        args = args[2:]

    if not args:
        print(HELP_TEXT)
        sys.exit(0)

    return account_id, args[0].lower()


def main() -> None:
    """Parse CLI arguments and dispatch."""
    account_id, agent_name = _parse_args()

    # Build AccountContext(s)
    if account_id == "all":
        # --account all: iterate over all registered accounts
        accounts = _list_accounts()
        if not accounts:
            print("[ERROR] No accounts found in accounts/ directory.", file=sys.stderr)
            sys.exit(1)

        if agent_name == "all":
            # daemon mode for all accounts
            _run_daemon(ctx=None)
        else:
            # single agent across all accounts (sequential, 5-min gap)
            import time as _time
            if agent_name not in _AGENT_REGISTRY:
                print(f"[ERROR] Unknown agent: '{agent_name}'", file=sys.stderr)
                sys.exit(1)
            for i, acct_id in enumerate(accounts):
                ctx = AccountContext(acct_id)
                _run_single(agent_name, ctx)
                if i < len(accounts) - 1:
                    logger.info("Waiting 300s before next account...")
                    _time.sleep(300)
    else:
        ctx = AccountContext(account_id)
        if agent_name == "all":
            _run_daemon(ctx)
        elif agent_name in _AGENT_REGISTRY:
            _run_single(agent_name, ctx)
        else:
            print(f"[ERROR] Unknown agent: '{agent_name}'", file=sys.stderr)
            print(HELP_TEXT, file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
