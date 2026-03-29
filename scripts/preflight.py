"""Pre-flight check for ThreadsBot production deployment.

Usage::

    python scripts/preflight.py           # run all checks
    python scripts/preflight.py --notify  # also send a test Telegram message

Verifies:
  1. Required environment variables
  2. Data directory and state files
  3. Config files are valid YAML
  4. Token status
  5. Telegram notification (with --notify)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK" if ok else "NG"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{mark}] {label}{suffix}")
    return ok


def check_env_vars() -> int:
    """Check required environment variables are set."""
    print("\n--- 1. Environment Variables ---")
    fails = 0

    required = [
        ("THREADS_ACCESS_TOKEN", "Threads API"),
        ("THREADS_USER_ID", "Threads API"),
        ("ANTHROPIC_API_KEY", "Claude API"),
    ]
    optional = [
        ("THREADS_APP_ID", "Token refresh (Meta OAuth)"),
        ("THREADS_APP_SECRET", "Token refresh (Meta OAuth)"),
        ("YOUTUBE_API_KEY", "YouTube Data API"),
        ("TELEGRAM_BOT_TOKEN", "Telegram notifications"),
        ("TELEGRAM_CHAT_ID", "Telegram notifications"),
    ]

    for var, purpose in required:
        val = os.environ.get(var, "")
        ok = bool(val and val != f"your_{var.lower()}_here")
        if not _check(f"{var} ({purpose})", ok, "REQUIRED"):
            fails += 1

    for var, purpose in optional:
        val = os.environ.get(var, "")
        ok = bool(val and not val.startswith("your_"))
        _check(f"{var} ({purpose})", ok, "optional" if not ok else "")

    return fails


def check_data_files() -> int:
    """Check that data/state/ files exist."""
    print("\n--- 2. Data Files ---")
    fails = 0

    state_dir = _PROJECT_ROOT / "data" / "state"
    if not state_dir.exists():
        print(f"  [NG] data/state/ directory missing — run: python scripts/init_data.py")
        return 1

    required_files = [
        "system_state.json",
        "post_history.json",
        "post_queue.json",
        "draft_queue.json",
        "research_pool.json",
    ]

    for fname in required_files:
        path = state_dir / fname
        ok = path.exists() and path.stat().st_size > 2
        if not _check(fname, ok, "run init_data.py" if not ok else f"{path.stat().st_size:,}B"):
            fails += 1

    return fails


def check_config_files() -> int:
    """Check config files are valid YAML."""
    print("\n--- 3. Config Files ---")
    import yaml

    fails = 0
    config_dir = _PROJECT_ROOT / "config"

    files = ["settings.yaml", "schedule.yaml", "tone.yaml", "ng_words.txt"]
    for fname in files:
        path = config_dir / fname
        exists = path.exists()
        valid = False
        if exists:
            if fname.endswith(".yaml"):
                try:
                    with open(path, encoding="utf-8") as f:
                        yaml.safe_load(f)
                    valid = True
                except yaml.YAMLError as exc:
                    _check(fname, False, f"YAML parse error: {exc}")
                    fails += 1
                    continue
            else:
                valid = True

        if not _check(fname, exists and valid, "" if valid else "missing"):
            fails += 1

    return fails


def check_token_status() -> int:
    """Check Threads API token health."""
    print("\n--- 4. Token Status ---")

    try:
        from core.state_manager import StateManager
        from services.token_manager import TokenManager

        sm = StateManager()
        tm = TokenManager(sm)
        status = tm.get_token_status()

        if status["status"] == "valid":
            _check(
                "Token",
                True,
                f"valid — {status['days_remaining']} days remaining",
            )
            return 0
        elif status["status"] == "expiring_soon":
            _check(
                "Token",
                True,
                f"expiring soon — {status['days_remaining']} days remaining (auto-refresh will trigger)",
            )
            return 0
        elif status["status"] == "expired":
            _check("Token", False, "EXPIRED — manual refresh required")
            return 1
        else:
            _check(
                "Token",
                False,
                "not tracked — run: python -c \"from services.token_manager import TokenManager; TokenManager().initialise_token_state()\"",
            )
            return 1
    except Exception as exc:
        _check("Token", False, str(exc))
        return 1


def check_telegram(send_test: bool = False) -> int:
    """Check Telegram notification setup."""
    print("\n--- 5. Telegram Notification ---")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        _check("Telegram", False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return 0  # Not a hard failure — notifications are optional

    _check("Telegram credentials", True, "configured")

    if send_test:
        try:
            from core.notifier import Notifier

            notifier = Notifier()
            ok = notifier.send(
                "preflight_test",
                "ThreadsBot preflight check — Telegram notification is working.",
                "low",
            )
            _check("Test message", ok, "sent" if ok else "failed to send")
            return 0 if ok else 1
        except Exception as exc:
            _check("Test message", False, str(exc))
            return 1

    return 0


def check_dependencies() -> int:
    """Check critical Python dependencies are importable."""
    print("\n--- 6. Dependencies ---")
    fails = 0

    deps = [
        "httpx",
        "yaml",
        "apscheduler",
        "sklearn",
        "dotenv",
        "pydantic",
    ]
    for dep in deps:
        try:
            __import__(dep)
            _check(dep, True)
        except ImportError:
            _check(dep, False, "not installed — run: pip install -r requirements.txt")
            fails += 1

    return fails


def main() -> None:
    send_test = "--notify" in sys.argv

    print("=" * 50)
    print("  ThreadsBot Pre-flight Check")
    print("=" * 50)

    total_fails = 0
    total_fails += check_env_vars()
    total_fails += check_data_files()
    total_fails += check_config_files()
    total_fails += check_token_status()
    total_fails += check_telegram(send_test=send_test)
    total_fails += check_dependencies()

    print("\n" + "=" * 50)
    if total_fails == 0:
        print("  ALL CHECKS PASSED — Ready for production!")
        print("  Start with: python -m core.scheduler all")
    else:
        print(f"  {total_fails} CHECK(S) FAILED — Fix issues above before launching.")
    print("=" * 50)

    sys.exit(0 if total_fails == 0 else 1)


if __name__ == "__main__":
    main()
