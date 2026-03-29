"""Safety guard: rate-limiting, emergency stop, circuit breaker, and compliance."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from core.boost import get_effective_safety_config
from core.logger import get_logger
from core.state_manager import StateManager

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = get_logger("safety")


class SafetyGuard:
    """Enforce safety constraints before posting.

    Reads parameters from ``config/settings.yaml`` and persists runtime
    state via :class:`StateManager`.
    """

    def __init__(self, state_manager: StateManager) -> None:
        self.sm = state_manager
        self.config = get_effective_safety_config(self._load_safety_config())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_post(self) -> tuple[bool, str]:
        """Determine whether a new post is allowed right now.

        Checks are executed in the following order:

        1. Emergency stop
        2. Daily post limit (``max_daily_posts``)
        3. Minimum interval (``min_post_interval_minutes``)
        4. Active-hours window (``active_hours``)

        Returns:
            ``(True, "ok")`` when posting is allowed, or
            ``(False, "<reason>")`` when it is not.
        """
        # 1. Emergency stop
        state = self.sm.load_json("system_state.json")
        if state.get("emergency_stop"):
            reason = state.get("emergency_stop_reason", "unknown")
            return False, f"emergency_stop: {reason}"

        # 2. Daily post limit
        today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
        counters: dict[str, Any] = state.get("daily_counters", {})
        today_counter = counters.get(today, {})
        post_count = today_counter.get("posts", 0)
        max_daily = self.config["max_daily_posts"]
        if post_count >= max_daily:
            return False, f"daily_limit_reached: {post_count}/{max_daily}"

        # 3. Minimum interval
        history = self.sm.load_json("post_history.json")
        posts = history.get("posts", [])
        if posts:
            last_ts = posts[-1].get("posted_at")
            if last_ts:
                last_time = datetime.fromisoformat(last_ts)
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
                min_interval = timedelta(
                    minutes=self.config["min_post_interval_minutes"]
                )
                elapsed = datetime.now(ZoneInfo("Asia/Tokyo")) - last_time
                if elapsed < min_interval:
                    remaining = min_interval - elapsed
                    return (
                        False,
                        f"min_interval: wait {int(remaining.total_seconds() // 60)}m",
                    )

        # 4. Active hours
        now_hour = datetime.now(ZoneInfo("Asia/Tokyo")).hour
        start = self.config["active_hours"]["start"]
        end = self.config["active_hours"]["end"]
        if not (start <= now_hour < end):
            return False, f"outside_active_hours: {start}:00-{end}:00"

        return True, "ok"

    def emergency_stop(self, reason: str) -> None:
        """Activate the emergency stop.

        Args:
            reason: Human-readable explanation for the stop.
        """
        state = self.sm.load_json("system_state.json")
        state["emergency_stop"] = True
        state["emergency_stop_reason"] = reason
        state["emergency_stop_at"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
        self.sm.save_json("system_state.json", state)
        logger.warning("EMERGENCY STOP activated: %s", reason)

    def clear_emergency_stop(self) -> None:
        """Manually clear the emergency stop."""
        state = self.sm.load_json("system_state.json")
        state["emergency_stop"] = False
        state["emergency_stop_reason"] = None
        state["emergency_stop_at"] = None
        self.sm.save_json("system_state.json", state)
        logger.info("Emergency stop cleared")

    def record_post(self) -> None:
        """Increment today's post counter in system_state."""
        today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")

        def _update(state: dict[str, Any]) -> None:
            counters = state.setdefault("daily_counters", {})
            today_counter = counters.setdefault(today, {})
            today_counter["posts"] = today_counter.get("posts", 0) + 1

        data = self.sm.locked_update("system_state.json", _update)
        count = data["daily_counters"][today]["posts"]
        logger.info("Post recorded — today total: %d", count)

    def record_error(self, agent_name: str, error: str) -> None:
        """Record an error for *agent_name* in agent_status.

        Consecutive errors are tracked for circuit-breaker evaluation.

        Args:
            agent_name: Identifier of the agent (e.g. ``"writer"``).
            error: Short description of the error.
        """
        now_str = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()

        def _update(state: dict[str, Any]) -> None:
            status = state.setdefault("agent_status", {})
            agent = status.setdefault(agent_name, {})
            agent["consecutive_errors"] = agent.get("consecutive_errors", 0) + 1
            agent["last_error"] = error
            agent["last_error_at"] = now_str

        data = self.sm.locked_update("system_state.json", _update)
        consecutive = data["agent_status"][agent_name]["consecutive_errors"]
        logger.warning(
            "Error recorded for %s (consecutive: %d): %s",
            agent_name,
            consecutive,
            error,
        )

    def is_circuit_open(self, agent_name: str) -> bool:
        """Check whether the circuit breaker is open for *agent_name*.

        The circuit opens when ``consecutive_errors`` reaches
        ``max_consecutive_errors``. It closes again after
        ``circuit_breaker_cooldown_minutes`` have elapsed since the last error.

        Args:
            agent_name: Identifier of the agent.

        Returns:
            ``True`` if the circuit is open (agent should NOT run).
        """
        state = self.sm.load_json("system_state.json")
        agent = state.get("agent_status", {}).get(agent_name, {})
        consecutive = agent.get("consecutive_errors", 0)
        max_errors = self.config["max_consecutive_errors"]

        if consecutive < max_errors:
            return False

        last_error_at = agent.get("last_error_at")
        if not last_error_at:
            return True

        cooldown = timedelta(
            minutes=self.config["circuit_breaker_cooldown_minutes"]
        )
        last_err_dt = datetime.fromisoformat(last_error_at)
        if last_err_dt.tzinfo is None:
            last_err_dt = last_err_dt.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        elapsed = datetime.now(ZoneInfo("Asia/Tokyo")) - last_err_dt
        if elapsed >= cooldown:
            # Cooldown expired — reset via locked_update to avoid race
            def _reset(s: dict[str, Any]) -> None:
                a = s.get("agent_status", {}).get(agent_name, {})
                a["consecutive_errors"] = 0

            self.sm.locked_update("system_state.json", _reset)
            logger.info("Circuit breaker reset for %s (cooldown expired)", agent_name)
            return False

        logger.warning("Circuit breaker OPEN for %s", agent_name)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_safety_config() -> dict[str, Any]:
        """Load the ``safety`` section from settings.yaml."""
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg["safety"]


class ComplianceChecker:
    """薬機法AI判定: Check content against pharmaceutical regulations using Claude Haiku.

    Uses a prompt template (``prompts/compliance_check.txt``) together with
    the official 56 permitted cosmetic claims list
    (``config/cosmetic_claims_56.txt``) to ask Claude whether a given post
    text violates Japan's Pharmaceutical and Medical Device Act (薬機法).

    Verdicts:

    * ``safe`` – all expressions fall within the 56 permitted claims.
    * ``violation`` – clearly claims effects outside the permitted scope.
    * ``borderline`` – ambiguous or could be interpreted as outside scope.

    Both ``violation`` and ``borderline`` set ``review_required=True``.
    """

    _PROMPT_PATH = _PROJECT_ROOT / "prompts" / "compliance_check.txt"
    _CLAIMS_PATH = _PROJECT_ROOT / "config" / "cosmetic_claims_56.txt"

    def __init__(self, claude_client: Any | None = None) -> None:
        self._claude_client = claude_client
        self._prompt_template: str | None = None
        self._claims_56: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, content: str) -> dict[str, Any]:
        """Check *content* for pharmaceutical compliance.

        Args:
            content: The post text to evaluate.

        Returns:
            A dict with keys ``verdict`` (``"safe"`` / ``"violation"`` /
            ``"borderline"``), ``reason`` (str), ``flagged_expressions``
            (list[str]), and ``review_required`` (bool).
        """
        try:
            client = self._get_client()
            prompt = self._build_prompt(content)
        except Exception:
            logger.exception("Failed to prepare compliance check")
            return self._fallback_result("プロンプト準備に失敗しました")

        logger.info("Running compliance check (content length=%d)", len(content))

        try:
            raw_response = client.prompt(prompt, max_tokens=512, timeout=60)
        except Exception:
            logger.exception("Claude CLI call failed during compliance check")
            return self._fallback_result("Claude API呼び出しに失敗しました")

        return self._parse_response(raw_response)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return the Claude client, creating a default one if needed."""
        if self._claude_client is None:
            from services.claude_client import ClaudeClient

            self._claude_client = ClaudeClient()
            logger.debug("Created default ClaudeClient instance")
        return self._claude_client

    def _load_prompt_template(self) -> str:
        """Load and cache the prompt template from disk.

        Raises:
            FileNotFoundError: If the prompt template file is missing.
        """
        if self._prompt_template is None:
            logger.debug("Loading prompt template from %s", self._PROMPT_PATH)
            if not self._PROMPT_PATH.exists():
                raise FileNotFoundError(f"Compliance prompt template not found: {self._PROMPT_PATH}")
            self._prompt_template = self._PROMPT_PATH.read_text(encoding="utf-8")
        return self._prompt_template

    def _load_claims_56(self) -> str:
        """Load and cache the 56 permitted cosmetic claims from disk.

        Raises:
            FileNotFoundError: If the claims file is missing.
        """
        if self._claims_56 is None:
            logger.debug("Loading cosmetic claims from %s", self._CLAIMS_PATH)
            if not self._CLAIMS_PATH.exists():
                raise FileNotFoundError(f"Cosmetic claims file not found: {self._CLAIMS_PATH}")
            self._claims_56 = self._CLAIMS_PATH.read_text(encoding="utf-8")
        return self._claims_56

    def _build_prompt(self, content: str) -> str:
        """Assemble the full prompt with claims and content injected."""
        template = self._load_prompt_template()
        claims = self._load_claims_56()
        return template.replace("{claims_56}", claims).replace("{content}", content)

    def _parse_response(self, raw: str) -> dict[str, Any]:
        """Parse the Claude JSON response into a result dict.

        If JSON parsing fails, returns a ``borderline`` fallback so that
        the content goes through human review rather than being silently
        approved.
        """
        try:
            # Try direct parse first
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Attempt to extract JSON from surrounding text
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    data = json.loads(raw[start:end])
                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse compliance JSON response: %s",
                        raw[:300],
                    )
                    return self._fallback_result("JSON解析に失敗しました")
            else:
                logger.warning(
                    "No JSON object found in compliance response: %s",
                    raw[:300],
                )
                return self._fallback_result("JSON解析に失敗しました")

        verdict = data.get("verdict", "borderline")
        if verdict not in ("safe", "violation", "borderline"):
            logger.warning("Unknown verdict '%s', treating as borderline", verdict)
            verdict = "borderline"

        reason = data.get("reason", "")
        flagged = data.get("flagged_expressions", [])
        if not isinstance(flagged, list):
            flagged = []
        review_required = verdict in ("violation", "borderline")

        logger.info(
            "Compliance result: verdict=%s, review_required=%s, flagged=%s",
            verdict,
            review_required,
            flagged,
        )
        return {
            "verdict": verdict,
            "reason": reason,
            "flagged_expressions": flagged,
            "review_required": review_required,
        }

    @staticmethod
    def _fallback_result(reason: str) -> dict[str, Any]:
        """Return a safe-fallback ``borderline`` result for error cases."""
        logger.warning("Compliance check fallback triggered: %s", reason)
        return {
            "verdict": "borderline",
            "reason": reason,
            "flagged_expressions": [],
            "review_required": True,
        }
