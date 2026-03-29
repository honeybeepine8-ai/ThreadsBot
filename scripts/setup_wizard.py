"""Interactive setup wizard for ThreadsBot.

Usage::

    python scripts/setup_wizard.py           # interactive setup
    python scripts/setup_wizard.py --check   # check-only (no prompts, no file creation)

Guides a new user through environment checks, directory creation,
API credential entry, config validation, and initial data setup.
"""

from __future__ import annotations

import getpass
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so core.* imports work.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# ANSI helpers (safe for Windows Terminal / PowerShell)
# ---------------------------------------------------------------------------

_COLOR = {
    "green": "\033[92m",
    "red": "\033[91m",
    "yellow": "\033[93m",
    "cyan": "\033[96m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _c(text: str, color: str) -> str:
    """Wrap *text* in ANSI colour codes."""
    return f"{_COLOR.get(color, '')}{text}{_COLOR['reset']}"


def _ok(label: str) -> str:
    return _c(f"  [OK]   {label}", "green")


def _ng(label: str) -> str:
    return _c(f"  [NG]   {label}", "red")


def _skip(label: str) -> str:
    return _c(f"  [SKIP] {label}", "yellow")


def _header(title: str) -> None:
    width = 60
    print()
    print(_c("=" * width, "cyan"))
    print(_c(f"  {title}", "bold"))
    print(_c("=" * width, "cyan"))


def _section(title: str) -> None:
    print()
    print(_c(f"--- {title} ---", "bold"))


# ---------------------------------------------------------------------------
# Result tracker
# ---------------------------------------------------------------------------

class _Results:
    """Collects OK / NG / SKIP results for the final summary."""

    def __init__(self) -> None:
        self._items: list[tuple[str, str, str]] = []  # (status, label, detail)

    def ok(self, label: str, detail: str = "") -> None:
        self._items.append(("OK", label, detail))
        print(_ok(f"{label}  {detail}"))

    def ng(self, label: str, detail: str = "") -> None:
        self._items.append(("NG", label, detail))
        print(_ng(f"{label}  {detail}"))

    def skip(self, label: str, detail: str = "") -> None:
        self._items.append(("SKIP", label, detail))
        print(_skip(f"{label}  {detail}"))

    def summary(self) -> None:
        _header("Setup Result Summary")
        ok_count = sum(1 for s, _, _ in self._items if s == "OK")
        ng_count = sum(1 for s, _, _ in self._items if s == "NG")
        skip_count = sum(1 for s, _, _ in self._items if s == "SKIP")
        for status, label, detail in self._items:
            if status == "OK":
                print(_ok(f"{label}  {detail}"))
            elif status == "NG":
                print(_ng(f"{label}  {detail}"))
            else:
                print(_skip(f"{label}  {detail}"))
        print()
        print(
            f"  Total: {_c(str(ok_count), 'green')} OK  "
            f"{_c(str(ng_count), 'red')} NG  "
            f"{_c(str(skip_count), 'yellow')} SKIP"
        )
        if ng_count == 0:
            print()
            print(_c("  All checks passed! ThreadsBot is ready.", "green"))
        else:
            print()
            print(
                _c(
                    f"  {ng_count} issue(s) need attention before running the bot.",
                    "red",
                )
            )


# ---------------------------------------------------------------------------
# Step 1: Environment check
# ---------------------------------------------------------------------------

_REQUIRED_PACKAGES: list[tuple[str, str]] = [
    ("httpx", "httpx"),
    ("yaml", "pyyaml"),
    ("sklearn", "scikit-learn"),
]


def check_python_version(res: _Results) -> None:
    v = sys.version_info
    label = "Python version"
    if v >= (3, 11):
        res.ok(label, f"{v.major}.{v.minor}.{v.micro}")
    else:
        res.ng(label, f"{v.major}.{v.minor}.{v.micro} (3.11+ required)")


def check_packages(res: _Results) -> None:
    for import_name, pip_name in _REQUIRED_PACKAGES:
        label = f"Package: {pip_name}"
        try:
            importlib.import_module(import_name)
            res.ok(label)
        except ImportError:
            res.ng(label, f"not found — pip install {pip_name}")


# ---------------------------------------------------------------------------
# Step 2: Data directories
# ---------------------------------------------------------------------------

_DATA_DIRS = [
    "data/state",
    "data/analytics",
    "data/logs",
    "data/backups",
]


def ensure_data_dirs(res: _Results, *, create: bool = True) -> None:
    for rel in _DATA_DIRS:
        d = _PROJECT_ROOT / rel
        label = f"Directory: {rel}/"
        if d.is_dir():
            res.ok(label)
        elif create:
            try:
                d.mkdir(parents=True, exist_ok=True)
                res.ok(label, "(created)")
            except OSError as exc:
                res.ng(label, str(exc))
        else:
            res.ng(label, "missing")


# ---------------------------------------------------------------------------
# Step 3: API credentials (.env)
# ---------------------------------------------------------------------------

_ENV_FILE = _PROJECT_ROOT / ".env"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"
_ENV_TEMPLATE = _PROJECT_ROOT / ".env.template"

# Keys that should have a real value (not empty / placeholder)
_REQUIRED_ENV_KEYS = ["THREADS_USER_ID", "THREADS_ACCESS_TOKEN"]
_OPTIONAL_ENV_KEYS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def _parse_env(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE lines, # comments, blanks ignored)."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _is_placeholder(value: str) -> bool:
    """Return True if *value* looks like a placeholder or is empty."""
    if not value:
        return True
    lower = value.lower()
    return lower.startswith("your_") or lower in ("", "none", "null")


def _find_template() -> Path | None:
    """Return the first existing template file, or None."""
    for candidate in (_ENV_TEMPLATE, _ENV_EXAMPLE):
        if candidate.is_file():
            return candidate
    return None


def _create_default_template() -> Path:
    """Create a minimal .env.template and return its path."""
    content = """\
# ============================================================
# ThreadsBot 環境変数テンプレート
# ============================================================

# --- Threads API (Meta) ---
THREADS_USER_ID=
THREADS_ACCESS_TOKEN=

# --- Claude API (Anthropic) ---
ANTHROPIC_API_KEY=

# --- 通知（任意） ---
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
"""
    _ENV_TEMPLATE.write_text(content, encoding="utf-8")
    return _ENV_TEMPLATE


def setup_env(res: _Results, *, interactive: bool = True) -> None:
    """Check / create .env and optionally prompt for missing credentials."""

    label_file = ".env file"

    if _ENV_FILE.is_file():
        res.ok(label_file, "exists")
    elif interactive:
        # Try to copy from template
        template = _find_template()
        if template is None:
            template = _create_default_template()
            print(f"    Created {template.relative_to(_PROJECT_ROOT)}")
        shutil.copy2(template, _ENV_FILE)
        res.ok(label_file, f"created from {template.name}")
    else:
        res.ng(label_file, "missing")
        return  # nothing more to check

    env = _parse_env(_ENV_FILE)

    # --- Required keys ---
    for key in _REQUIRED_ENV_KEYS:
        label = f"Env: {key}"
        val = env.get(key, "")
        if _is_placeholder(val):
            if interactive:
                entered = _prompt_secret(key)
                if entered:
                    _upsert_env_key(key, entered)
                    res.ok(label, "(set)")
                else:
                    res.ng(label, "empty — required")
            else:
                res.ng(label, "not set")
        else:
            res.ok(label)

    # --- Optional keys ---
    for key in _OPTIONAL_ENV_KEYS:
        label = f"Env: {key} (optional)"
        val = env.get(key, "")
        if _is_placeholder(val):
            if interactive:
                print(f"\n    {key} is optional (Telegram notification).")
                ans = input("    Set now? [y/N]: ").strip().lower()
                if ans == "y":
                    entered = _prompt_secret(key)
                    if entered:
                        _upsert_env_key(key, entered)
                        res.ok(label, "(set)")
                    else:
                        res.skip(label, "skipped")
                else:
                    res.skip(label, "skipped")
            else:
                res.skip(label, "not set")
        else:
            res.ok(label)


def _prompt_secret(key: str) -> str:
    """Prompt the user for a secret value using getpass (hidden input)."""
    print()
    return getpass.getpass(f"    Enter {key} (input hidden): ").strip()


def _upsert_env_key(key: str, value: str) -> None:
    """Insert or update a KEY=VALUE pair in the .env file."""
    lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _ = stripped.partition("=")
        if k.strip() == key:
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 4: Config files
# ---------------------------------------------------------------------------

_CONFIG_FILES = [
    "config/settings.yaml",
    "config/tone.yaml",
    "config/ng_words.txt",
    "config/schedule.yaml",
    "config/brand_names.txt",
]


def check_config_files(res: _Results) -> None:
    for rel in _CONFIG_FILES:
        p = _PROJECT_ROOT / rel
        label = f"Config: {rel}"
        if p.is_file():
            res.ok(label)
        else:
            res.ng(label, "missing")


# ---------------------------------------------------------------------------
# Step 5: Knowledge files
# ---------------------------------------------------------------------------

_KNOWLEDGE_FILES = [
    "knowledge/profile.yaml",
    "knowledge/target.yaml",
    "knowledge/genre.yaml",
    "knowledge/domain_knowledge.md",
    "knowledge/theme_tree.yaml",
    "knowledge/posting_rules.md",
    "knowledge/hook_stock.json",
    "knowledge/season_matrix.yaml",
    "knowledge/debate_whitelist.yaml",
]


def check_knowledge_files(res: _Results) -> None:
    for rel in _KNOWLEDGE_FILES:
        p = _PROJECT_ROOT / rel
        label = f"Knowledge: {rel}"
        if p.is_file():
            res.ok(label)
        else:
            res.ng(label, "missing")


# ---------------------------------------------------------------------------
# Step 6: Initial state data
# ---------------------------------------------------------------------------

_INIT_DATA_SCRIPT = _PROJECT_ROOT / "scripts" / "init_data.py"


def run_init_data(res: _Results, *, execute: bool = True) -> None:
    label = "scripts/init_data.py"
    if not _INIT_DATA_SCRIPT.is_file():
        res.skip(label, "not found — skipped")
        return

    if not execute:
        res.ok(label, "exists (not executed in --check mode)")
        return

    try:
        result = subprocess.run(
            [sys.executable, str(_INIT_DATA_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            res.ok(label, "executed successfully")
        else:
            stderr = result.stderr.strip()[:200] if result.stderr else ""
            res.ng(label, f"exit code {result.returncode}: {stderr}")
    except subprocess.TimeoutExpired:
        res.ng(label, "timed out (30s)")
    except OSError as exc:
        res.ng(label, str(exc))


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def run_setup(*, check_only: bool = False) -> None:
    """Execute the full setup wizard.

    Parameters
    ----------
    check_only:
        If True, run all checks but never prompt for input and never
        create or modify files.
    """
    interactive = not check_only

    if interactive:
        _header("ThreadsBot Setup Wizard")
        print("  This wizard will guide you through the initial setup.")
        print("  Press Ctrl+C at any time to abort.")
    else:
        _header("ThreadsBot Setup Check")
        print("  Running in check-only mode (no changes will be made).")

    res = _Results()

    # Step 1 — Environment
    _section("Step 1: Environment Check")
    check_python_version(res)
    check_packages(res)

    # Step 2 — Data directories
    _section("Step 2: Data Directories")
    ensure_data_dirs(res, create=interactive)

    # Step 3 — API credentials
    _section("Step 3: API Credentials (.env)")
    setup_env(res, interactive=interactive)

    # Step 4 — Config files
    _section("Step 4: Config Files")
    check_config_files(res)

    # Step 5 — Knowledge files
    _section("Step 5: Knowledge Files")
    check_knowledge_files(res)

    # Step 6 — Init data
    _section("Step 6: Initial State Data")
    run_init_data(res, execute=interactive)

    # Step 7 — Summary
    res.summary()


def main() -> None:
    check_only = "--check" in sys.argv[1:]
    try:
        run_setup(check_only=check_only)
    except KeyboardInterrupt:
        print("\n\n  Aborted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
