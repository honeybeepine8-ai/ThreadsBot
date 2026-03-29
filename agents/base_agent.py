"""Base agent class providing shared lifecycle, retry logic, and safety checks."""

from __future__ import annotations

import time
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Any

from core.account_context import AccountContext
from core.logger import get_logger
from core.safety import SafetyGuard
from services.threads_api import RateLimitError, AuthenticationError


class BaseAgent(ABC):
    """Abstract base class for all ThreadsBot agents.

    Subclasses must implement :meth:`execute`.  The :meth:`run` method wraps
    ``execute()`` with emergency-stop checks, circuit-breaker checks, and
    automatic retry with exponential back-off.
    """

    MAX_RETRIES: int = 3
    RETRY_DELAYS: list[int] = [30, 120, 300]  # seconds

    def __init__(self, name: str, ctx: AccountContext | None = None) -> None:
        self.name = name
        self.logger = get_logger(name)
        self.ctx = ctx or AccountContext("default")
        self.state = self.ctx.get_state_manager()
        self.safety = SafetyGuard(self.state)

    # ------------------------------------------------------------------
    # File I/O helpers (delegates to AccountContext)
    # ------------------------------------------------------------------

    def _resolve_path(self, relative_path: str) -> Path:
        """Resolve a file path via the account context (account-first, project fallback)."""
        return self.ctx.resolve_path(relative_path)

    def _load_yaml(self, relative_path: str) -> dict[str, Any]:
        """Load a YAML file via the account context."""
        return self.ctx.load_yaml(relative_path)

    def _load_text(self, relative_path: str) -> str:
        """Load a text file via the account context."""
        return self.ctx.load_text(relative_path)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the agent with safety checks and retry logic.

        Flow:
        1. Check emergency stop flag.
        2. Check circuit breaker state.
        3. Call :meth:`execute` (implemented by subclass).
        4. On success, update agent status to ``"ok"``.
        5. On failure, apply retry strategy based on exception type.
        """
        self.logger.info("Agent '%s' starting run", self.name)

        # 1. Emergency-stop check
        if self.safety.is_circuit_open(self.name):
            self.logger.warning(
                "Circuit breaker is open. Skipping execution for '%s'.", self.name
            )
            return

        system_state = self.state.load_json("system_state.json")
        if system_state.get("emergency_stop"):
            self.logger.warning(
                "Emergency stop is active. Skipping execution for '%s'.", self.name
            )
            return

        # 2. Execute with retries
        for attempt in range(self.MAX_RETRIES):
            try:
                self.execute()
                # Success
                self.update_status("ok")
                self.logger.info("Agent '%s' completed successfully.", self.name)
                return

            except RateLimitError as exc:
                delay = self.RETRY_DELAYS[attempt] if attempt < len(self.RETRY_DELAYS) else self.RETRY_DELAYS[-1]
                self.logger.warning(
                    "RateLimitError on attempt %d/%d for '%s': %s — retrying in %ds",
                    attempt + 1,
                    self.MAX_RETRIES,
                    self.name,
                    exc,
                    delay,
                )
                time.sleep(delay)

            except AuthenticationError as exc:
                self.logger.critical(
                    "AuthenticationError for '%s': %s — triggering emergency stop.",
                    self.name,
                    exc,
                )
                self.safety.emergency_stop(
                    f"Authentication failure in agent '{self.name}': {exc}"
                )
                self.update_status("error", str(exc))
                return

            except Exception as exc:
                self.safety.record_error(self.name, str(exc))
                delay = self.RETRY_DELAYS[attempt] if attempt < len(self.RETRY_DELAYS) else self.RETRY_DELAYS[-1]
                self.logger.error(
                    "Unexpected error on attempt %d/%d for '%s': %s",
                    attempt + 1,
                    self.MAX_RETRIES,
                    self.name,
                    exc,
                    exc_info=True,
                )
                if attempt >= self.MAX_RETRIES - 1:
                    self.logger.critical(
                        "Max retries exhausted for '%s'. Triggering emergency stop.",
                        self.name,
                    )
                    self.safety.emergency_stop(
                        f"Max retries exhausted in agent '{self.name}': {exc}"
                    )
                    self.update_status("error", str(exc))
                    return
                time.sleep(delay)

    # ------------------------------------------------------------------
    # Abstract method
    # ------------------------------------------------------------------

    @abstractmethod
    def execute(self) -> None:
        """Perform the agent's main task.  Implemented by each subclass."""

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def update_status(self, status: str, error: str | None = None) -> None:
        """Update this agent's entry in ``system_state.json``'s ``agent_status``.

        Args:
            status: Short status string, e.g. ``"ok"`` or ``"error"``.
            error: Optional error message to persist.
        """
        import datetime
        from zoneinfo import ZoneInfo

        now_jst = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))

        system_state = self.state.load_json("system_state.json")
        agent_status = system_state.setdefault("agent_status", {})
        agent_status[self.name] = {
            "last_run": now_jst.isoformat(),
            "status": status,
            "error": error,
        }
        self.state.save_json("system_state.json", system_state)
