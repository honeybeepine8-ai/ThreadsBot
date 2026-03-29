"""Supervisor agent: health checks, anomaly detection, and alerting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import yaml

from agents.base_agent import BaseAgent
from core.boost import is_boost_active, load_boost_config
from core.constants import JST
from core.logger import get_logger
from core.notifier import Notifier, SEVERITY_MEDIUM, SEVERITY_HIGH, SEVERITY_CRITICAL
from services.token_manager import TokenManager

logger = get_logger("supervisor")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_POST_HISTORY_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "state" / "post_history.json"
)

# Expected maximum run intervals per agent (hours).
# If an agent has not run within 2x this value, it is considered stale.
_AGENT_EXPECTED_INTERVALS: dict[str, timedelta] = {
    "poster": timedelta(hours=2),
    "fetcher": timedelta(hours=12),
    "writer": timedelta(hours=4),
    "researcher": timedelta(hours=8),
    "analyst": timedelta(hours=48),
}


class SupervisorAgent(BaseAgent):
    """Monitor system health and detect anomalies.

    The supervisor runs every 15 minutes.  It checks:

    * Whether each agent ran within its expected schedule.
    * Whether posting patterns show anomalies (too frequent, same
      category streaks, etc.).
    * Whether the error rate is unacceptably high.

    Alerts are written to the log at ``ERROR`` level.  If three or more
    alerts fire simultaneously, the supervisor triggers an emergency stop.
    """

    def __init__(self) -> None:
        super().__init__("supervisor")
        self.config = self._load_supervisor_config()
        self.notifier = Notifier()

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    def execute(self) -> None:
        """Run all health-check routines and issue alerts if needed.

        Steps:
            1. Check agent health (staleness, error status).
            2. Check posting anomalies.
            3. Check error rate.
            4. Update ``last_health_check`` in ``system_state.json``.
        """
        alerts: list[str] = []

        alerts.extend(self._check_agent_health())
        alerts.extend(self._check_posting_anomalies())
        alerts.extend(self._check_error_rate())
        alerts.extend(self._check_token_expiry())
        alerts.extend(self._check_boost_mode_expiry())

        if alerts:
            self._send_alert(alerts)

        # Update health-check timestamp
        now = datetime.now(JST)
        system_state = self.state.load_json("system_state.json")
        system_state["last_health_check"] = now.isoformat()
        self.state.save_json("system_state.json", system_state)

        self.logger.info(
            "Health check complete. Alerts issued: %d", len(alerts)
        )

    # ------------------------------------------------------------------
    # Agent health
    # ------------------------------------------------------------------

    def _check_agent_health(self) -> list[str]:
        """Verify that every agent ran within its expected interval.

        Returns:
            A list of human-readable alert strings (empty if healthy).
        """
        alerts: list[str] = []
        now = datetime.now(JST)

        system_state = self.state.load_json("system_state.json")
        agent_status: dict[str, Any] = system_state.get("agent_status", {})

        for agent_name, max_interval in _AGENT_EXPECTED_INTERVALS.items():
            info: dict[str, Any] = agent_status.get(agent_name, {})

            # Check for error status
            if info.get("status") == "error":
                error_msg = info.get("error", "unknown")
                alerts.append(
                    f"Agent '{agent_name}' is in error state: {error_msg}"
                )

            # Check staleness
            last_run_str: str | None = info.get("last_run")
            if last_run_str is None:
                # Agent has never run — only warn if it is expected to have
                # run at least once by now (grace period = max_interval).
                self.logger.debug(
                    "Agent '%s' has no last_run recorded.", agent_name
                )
                continue

            try:
                last_run = datetime.fromisoformat(last_run_str)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=JST)
            except (ValueError, TypeError):
                alerts.append(
                    f"Agent '{agent_name}' has invalid last_run timestamp: "
                    f"{last_run_str}"
                )
                continue

            elapsed = now - last_run
            if elapsed > max_interval:
                alerts.append(
                    f"Agent '{agent_name}' is stale: last ran "
                    f"{elapsed.total_seconds() / 3600:.1f}h ago "
                    f"(threshold: {max_interval.total_seconds() / 3600:.0f}h)"
                )

        return alerts

    # ------------------------------------------------------------------
    # Posting anomalies
    # ------------------------------------------------------------------

    def _check_posting_anomalies(self) -> list[str]:
        """Detect anomalous posting patterns in the last 24 hours.

        Anomaly rules:
            * Two posts less than 30 minutes apart.
            * Three or more posts within the same clock hour.
            * Five consecutive posts with the same category.

        Returns:
            A list of alert strings.
        """
        alerts: list[str] = []
        now = datetime.now(JST)
        cutoff = now - timedelta(hours=24)

        history = self._load_post_history()
        all_posts: list[dict[str, Any]] = history.get("posts", [])

        # Filter to last 24 h
        recent_posts: list[dict[str, Any]] = []
        for p in all_posts:
            posted_at_str = p.get("posted_at")
            if not posted_at_str:
                continue
            try:
                dt = datetime.fromisoformat(posted_at_str)
            except (ValueError, TypeError):
                continue
            if dt >= cutoff:
                recent_posts.append({**p, "_dt": dt})

        # Sort by time ascending
        recent_posts.sort(key=lambda p: p["_dt"])

        if not recent_posts:
            return alerts

        # Rule 1: interval < 30 min between consecutive posts
        for i in range(1, len(recent_posts)):
            interval = recent_posts[i]["_dt"] - recent_posts[i - 1]["_dt"]
            if interval < timedelta(minutes=30):
                alerts.append(
                    f"Posts too close together: "
                    f"{recent_posts[i - 1].get('id', '?')} and "
                    f"{recent_posts[i].get('id', '?')} are only "
                    f"{int(interval.total_seconds() // 60)}min apart"
                )
                self.logger.warning(
                    "Posting interval violation: %d min between posts",
                    int(interval.total_seconds() // 60),
                )

        # Rule 2: 3+ posts in the same clock hour
        hour_counts: dict[str, int] = {}
        for p in recent_posts:
            key = p["_dt"].strftime("%Y-%m-%d-%H")
            hour_counts[key] = hour_counts.get(key, 0) + 1

        for key, count in hour_counts.items():
            if count >= 3:
                alerts.append(
                    f"Too many posts in hour {key}: {count} posts"
                )
                self.logger.warning(
                    "Burst posting detected: %d posts in hour %s", count, key
                )

        # Rule 3: 5 consecutive posts with the same category
        categories = [p.get("category", "unknown") for p in recent_posts]
        streak = 1
        for i in range(1, len(categories)):
            if categories[i] == categories[i - 1]:
                streak += 1
                if streak >= 5:
                    alerts.append(
                        f"Category streak: '{categories[i]}' posted "
                        f"{streak} times consecutively"
                    )
                    self.logger.warning(
                        "Category streak detected: '%s' x%d",
                        categories[i],
                        streak,
                    )
                    break
            else:
                streak = 1

        return alerts

    # ------------------------------------------------------------------
    # Error rate
    # ------------------------------------------------------------------

    def _check_error_rate(self) -> list[str]:
        """Check whether daily error rates exceed acceptable thresholds.

        Rules:
            * ``api_errors >= 50%`` of ``posts_published`` -> warning.
            * ``posts_rejected_quality + posts_rejected_similarity >= 80%``
              of total generation attempts -> warning (suggests quality
              settings need review).

        Returns:
            A list of alert strings.
        """
        alerts: list[str] = []

        system_state = self.state.load_json("system_state.json")
        daily_counters: dict[str, Any] = system_state.get("daily_counters", {})

        today = datetime.now(JST).strftime("%Y-%m-%d")
        today_counters: dict[str, int] = daily_counters.get(today, {})

        posts_published = today_counters.get("posts_published", 0)
        api_errors = today_counters.get("api_errors", 0)
        rejected_quality = today_counters.get("posts_rejected_quality", 0)
        rejected_similarity = today_counters.get("posts_rejected_similarity", 0)

        # Rule 1: API error rate
        if posts_published > 0 and api_errors >= posts_published * 0.5:
            alerts.append(
                f"High API error rate: {api_errors} errors vs "
                f"{posts_published} posts published today"
            )
            self.logger.warning(
                "API error rate critical: %d errors / %d posts",
                api_errors,
                posts_published,
            )

        # Rule 2: Rejection rate
        total_rejected = rejected_quality + rejected_similarity
        total_generated = posts_published + total_rejected
        if total_generated > 0 and total_rejected >= total_generated * 0.8:
            alerts.append(
                f"High rejection rate: {total_rejected}/{total_generated} "
                f"({total_rejected / total_generated * 100:.0f}%) posts rejected "
                f"(quality={rejected_quality}, similarity={rejected_similarity}). "
                f"Consider reviewing quality/similarity thresholds."
            )
            self.logger.warning(
                "Rejection rate critical: %d/%d rejected",
                total_rejected,
                total_generated,
            )

        return alerts

    # ------------------------------------------------------------------
    # Token expiry
    # ------------------------------------------------------------------

    def _check_token_expiry(self) -> list[str]:
        """Check token expiry and attempt auto-refresh if needed.

        When the token is approaching expiry, this method proactively
        calls :meth:`TokenManager.get_valid_token` to trigger a refresh.
        This ensures the token stays valid even during quiet periods when
        Poster/Replier may not be running.

        Returns:
            A list of alert strings (at most one).
        """
        alerts: list[str] = []
        try:
            tm = TokenManager(self.state)
            status = tm.get_token_status()

            if status["status"] == "expired":
                # Notify directly (don't add to alerts to avoid double notification)
                self.logger.error("Threads API token has EXPIRED.")
                self.notifier.send(
                    "token_expired",
                    "Threads APIトークンが期限切れです。手動更新が必要です。",
                    SEVERITY_CRITICAL,
                )
            elif status["status"] == "expiring_soon":
                # Attempt auto-refresh
                try:
                    tm.get_valid_token()
                    self.logger.info("Token auto-refreshed by Supervisor.")
                    self.notifier.send(
                        "token_refreshed",
                        f"トークンを自動更新しました（残り{status['days_remaining']}日で更新）",
                        SEVERITY_MEDIUM,
                    )
                except Exception as refresh_exc:
                    self.logger.error(
                        "Token auto-refresh failed: %s", refresh_exc
                    )
                    self.notifier.send(
                        "token_refresh_failed",
                        f"トークン自動更新失敗（残り{status['days_remaining']}日）: {refresh_exc}",
                        SEVERITY_CRITICAL,
                    )
            elif status["status"] == "unknown":
                self.logger.debug("Token expiry not tracked yet.")
        except Exception as exc:
            self.logger.warning("Token expiry check failed: %s", exc)

        return alerts

    # ------------------------------------------------------------------
    # Boost mode expiry
    # ------------------------------------------------------------------

    def _check_boost_mode_expiry(self) -> list[str]:
        """Detect when boost mode has expired and disable it in settings.

        When ``boost_mode.enabled`` is ``true`` but the period has elapsed,
        this method sets ``enabled`` to ``false`` in ``settings.yaml`` so the
        system automatically transitions back to normal mode.

        Returns:
            A list with one info-level alert if boost was disabled, else empty.
        """
        alerts: list[str] = []
        try:
            boost = load_boost_config()
            if not boost.get("enabled"):
                return alerts

            # If enabled but no longer active, the period has elapsed
            if not is_boost_active():
                self._disable_boost_mode()
                alerts.append(
                    "Boost mode expired. Automatically switched to normal mode."
                )
                self.logger.info("Boost mode expired — disabled in settings.yaml.")
        except Exception as exc:
            self.logger.warning("Boost mode expiry check failed: %s", exc)

        return alerts

    @staticmethod
    def _disable_boost_mode() -> None:
        """Set ``boost_mode.enabled`` to ``false`` in settings.yaml.

        Uses targeted regex replacement instead of yaml.dump to preserve
        comments, formatting, and blank lines in the config file.
        """
        import re

        config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        new_content = re.sub(
            r"(boost_mode:\s*\n\s+enabled:\s*)true",
            r"\1false",
            content,
        )

        if new_content != content:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(new_content)

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    def _send_alert(self, alerts: list[str]) -> None:
        """Process collected alerts.

        * Each alert is logged at ``ERROR`` level.
        * Each alert is forwarded to Telegram via :class:`Notifier`.
        * If three or more alerts fire simultaneously, an emergency stop
          is triggered.

        Args:
            alerts: List of human-readable alert strings.
        """
        for alert in alerts:
            self.logger.error("ALERT: %s", alert)

            # Determine severity from alert content
            severity = self._classify_alert_severity(alert)
            self.notifier.send("supervisor_alert", alert, severity)

        if len(alerts) >= 3:
            reason = (
                f"Supervisor detected {len(alerts)} simultaneous alerts: "
                + "; ".join(alerts[:5])
            )
            self.logger.critical(
                "Multiple alerts (%d) — triggering emergency stop.", len(alerts)
            )
            self.notifier.send(
                "emergency_stop",
                f"Emergency stop triggered: {len(alerts)} simultaneous alerts",
                SEVERITY_CRITICAL,
            )
            self.safety.emergency_stop(reason)

        self.logger.info(
            "Alert processing complete. Total alerts: %d. "
            "Emergency stop triggered: %s",
            len(alerts),
            len(alerts) >= 3,
        )

    @staticmethod
    def _classify_alert_severity(alert: str) -> str:
        """Map alert text to a notification severity level."""
        alert_lower = alert.lower()
        if "expired" in alert_lower or "emergency" in alert_lower:
            return SEVERITY_CRITICAL
        if (
            "circuit" in alert_lower
            or "error rate" in alert_lower
            or "expiring" in alert_lower
            or "token" in alert_lower
        ):
            return SEVERITY_HIGH
        return SEVERITY_MEDIUM

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------

    def _load_post_history(self) -> dict[str, Any]:
        """Load ``data/state/post_history.json``.

        Returns:
            The parsed post-history dict, or a default structure if the
            file is absent.
        """
        return self.state.load_json("post_history.json")

    # ------------------------------------------------------------------
    # Config loader
    # ------------------------------------------------------------------

    @staticmethod
    def _load_supervisor_config() -> dict[str, Any]:
        """Load the ``supervisor`` section from settings.yaml."""
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("supervisor", {})
