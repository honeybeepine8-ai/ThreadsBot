"""Emergency stop manual trigger for ThreadsBot.

Usage::

    python scripts/kill_switch.py stop "Reason for stopping"
    python scripts/kill_switch.py clear
    python scripts/kill_switch.py status
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path so core.* imports work.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.state_manager import StateManager
from core.safety import SafetyGuard

HELP_TEXT = """\
ThreadsBot Kill Switch
======================

Usage:
  python scripts/kill_switch.py stop "reason"   Activate emergency stop
  python scripts/kill_switch.py clear            Clear emergency stop
  python scripts/kill_switch.py status           Show current status
"""


def _get_system_state(sm: StateManager) -> dict:
    """Load and return the current system state."""
    return sm.load_json("system_state.json")


def _print_status(sm: StateManager) -> None:
    """Print the current emergency stop status to stdout."""
    state = _get_system_state(sm)
    is_stopped = state.get("emergency_stop", False)
    reason = state.get("emergency_stop_reason", "-")
    stopped_at = state.get("emergency_stop_at", "-")

    print()
    print("=== ThreadsBot System Status ===")
    if is_stopped:
        print(f"  Emergency Stop : ACTIVE")
        print(f"  Reason         : {reason}")
        print(f"  Stopped At     : {stopped_at}")
    else:
        print(f"  Emergency Stop : inactive")
    print()


def cmd_status(sm: StateManager, guard: SafetyGuard) -> None:
    """Display system status."""
    _print_status(sm)


def cmd_stop(sm: StateManager, guard: SafetyGuard, reason: str) -> None:
    """Activate emergency stop with confirmation."""
    _print_status(sm)

    state = _get_system_state(sm)
    if state.get("emergency_stop"):
        print("[WARN] Emergency stop is already active.")
        print()

    print(f'About to activate emergency stop with reason: "{reason}"')
    answer = input("Are you sure? (yes/no): ").strip().lower()

    if answer not in ("yes", "y"):
        print("Aborted.")
        return

    guard.emergency_stop(reason)
    print()
    print("[OK] Emergency stop activated.")
    _print_status(sm)


def cmd_clear(sm: StateManager, guard: SafetyGuard) -> None:
    """Clear emergency stop."""
    _print_status(sm)

    state = _get_system_state(sm)
    if not state.get("emergency_stop"):
        print("Emergency stop is not active. Nothing to clear.")
        return

    guard.clear_emergency_stop()
    print("[OK] Emergency stop cleared.")
    _print_status(sm)


def main() -> None:
    """Parse arguments and dispatch to the appropriate sub-command."""
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        sys.exit(0)

    command = sys.argv[1].lower()
    sm = StateManager()
    guard = SafetyGuard(sm)

    if command == "status":
        cmd_status(sm, guard)

    elif command == "stop":
        if len(sys.argv) < 3:
            print("[ERROR] 'stop' requires a reason.", file=sys.stderr)
            print('  Usage: python scripts/kill_switch.py stop "reason"', file=sys.stderr)
            sys.exit(1)
        reason = sys.argv[2]
        cmd_stop(sm, guard, reason)

    elif command == "clear":
        cmd_clear(sm, guard)

    else:
        print(f"[ERROR] Unknown command: '{command}'", file=sys.stderr)
        print(HELP_TEXT, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
