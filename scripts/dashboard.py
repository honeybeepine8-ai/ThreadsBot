"""Dashboard CLI — display current ThreadsBot status at a glance.

Usage::

    python scripts/dashboard.py              # one-shot display
    python scripts/dashboard.py --watch      # auto-refresh every 30 seconds
"""

from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import yaml

from core.state_manager import StateManager

_SETTINGS_PATH = _PROJECT_ROOT / "config" / "settings.yaml"


def _relative_time(iso_str: str | None) -> str:
    """Convert ISO datetime string to a relative time string like '3分前'."""
    if not iso_str:
        return "---"
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=datetime.timezone(datetime.timedelta(hours=9))
            )
        now = datetime.datetime.now(dt.tzinfo)
        diff = now - dt
        seconds = int(diff.total_seconds())

        if seconds < 0:
            return "未来"
        if seconds < 60:
            return f"{seconds}秒前"
        if seconds < 3600:
            return f"{seconds // 60}分前"
        if seconds < 86400:
            return f"{seconds // 3600}時間前"
        return f"{seconds // 86400}日前"
    except (ValueError, TypeError):
        return "---"


def _count_items(data: dict[str, Any], key: str, status: str | None = None) -> int:
    """Count items in a list, optionally filtered by status."""
    items = data.get(key, [])
    if status:
        return sum(1 for item in items if item.get("status") == status)
    return len(items)


def render_dashboard(sm: StateManager) -> str:
    """Build the dashboard text from current state files.

    Returns:
        Formatted dashboard string ready for printing.
    """
    # Load settings
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            settings = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        settings = {}
    max_daily_posts = settings.get("safety", {}).get("max_daily_posts", 6)

    # Load all state data
    system = sm.load_json("system_state.json")
    draft_data = sm.load_json("draft_queue.json")
    post_queue = sm.load_json("post_queue.json")
    research_pool = sm.load_json("research_pool.json")
    performance = sm.load_json("performance.json")
    token_state = sm.load_json("token_state.json")

    # Status line
    heartbeat = system.get("last_health_check")
    emergency = system.get("emergency_stop", False)
    status_str = "STOPPED" if emergency else "RUNNING"
    heartbeat_str = _relative_time(heartbeat)

    # Today's post count
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_key = now.strftime("%Y-%m-%d")
    daily_counters = system.get("daily_counters", {})
    today_posts = daily_counters.get(today_key, {}).get("posts", 0)

    # Next scheduled post
    pending_posts = [
        p for p in post_queue.get("queue", [])
        if p.get("status") == "pending"
    ]
    next_post = "---"
    if pending_posts:
        next_scheduled = min(
            (p.get("scheduled_at", "") for p in pending_posts),
            default="",
        )
        if next_scheduled:
            try:
                dt = datetime.datetime.fromisoformat(next_scheduled)
                next_post = dt.strftime("%H:%M JST")
            except ValueError:
                pass

    # Queue counts
    draft_pending = _count_items(draft_data, "drafts", "pending_review")
    post_pending = len(pending_posts)
    research_count = len([
        i for i in research_pool.get("items", []) if not i.get("used")
    ])

    # Agent status
    agent_status = system.get("agent_status", {})
    agent_names = ["researcher", "writer", "poster", "fetcher", "analyst", "replier", "supervisor"]
    agent_intervals = {
        "researcher": "4h間隔",
        "writer": "オンデマンド",
        "poster": "1h間隔",
        "fetcher": "6h間隔",
        "analyst": "日次",
        "replier": "30min間隔",
        "supervisor": "15min間隔",
    }

    # Performance (7d)
    perf_summary = performance.get("summary", {})
    avg_engagement = perf_summary.get("avg_engagement_rate", 0)
    top_category = perf_summary.get("top_category", "---")
    entropy = perf_summary.get("category_entropy", 0)
    followers = perf_summary.get("followers_total", "---")
    followers_change = perf_summary.get("followers_change_7d", 0)
    followers_sign = "+" if followers_change >= 0 else ""

    # Token expiry
    token_expires = token_state.get("expires_at")
    token_days = "---"
    if token_expires:
        try:
            exp_dt = datetime.datetime.fromisoformat(token_expires)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(
                    tzinfo=datetime.timezone(datetime.timedelta(hours=9))
                )
            days_left = (exp_dt - now).days
            token_days = f"{days_left} days"
        except (ValueError, TypeError):
            pass

    # Circuit breaker
    cb_active = False
    cb_state = system.get("circuit_breaker", {})
    for _agent, cb_info in cb_state.items():
        if cb_info.get("open", False):
            cb_active = True
            break

    # Build output
    lines = [
        f"=== ThreadsBot Dashboard ===",
        f"Status    : {status_str} (heartbeat: {heartbeat_str})",
        f"Today     : {today_posts}/{max_daily_posts} posts | Next: {next_post}",
        f"Queues    : draft {draft_pending}件 / post {post_pending}件 / research {research_count}件",
        "",
        "--- Agents ---",
    ]

    for name in agent_names:
        info = agent_status.get(name, {})
        last_run = info.get("last_run", "")
        status = info.get("status", "---")
        interval = agent_intervals.get(name, "")

        if last_run:
            try:
                dt = datetime.datetime.fromisoformat(last_run)
                time_str = dt.strftime("%H:%M")
            except ValueError:
                time_str = "??:??"
        else:
            time_str = "--:--"

        status_icon = "OK" if status == "ok" else status.upper() if status else "---"
        lines.append(f"  {name:12s}: {time_str} {status_icon:8s} ({interval})")

    lines.extend([
        "",
        "--- Performance (7d) ---",
        f"  Avg Engagement : {avg_engagement:.1f}%",
        f"  Top Category   : {top_category}",
        f"  Entropy        : {entropy:.2f} / 2.32 (目標)",
        f"  Followers      : {followers} ({followers_sign}{followers_change} this week)",
        "",
        "--- Alerts ---",
    ])

    # Alerts
    alerts: list[str] = []
    if emergency:
        reason = system.get("emergency_stop_reason", "不明")
        alerts.append(f"  [CRITICAL] Emergency Stop: {reason}")
    if cb_active:
        alerts.append("  [ALERT] Circuit Breaker active")

    # Check research pool staleness
    rp_updated = research_pool.get("last_updated")
    if rp_updated:
        try:
            rp_dt = datetime.datetime.fromisoformat(rp_updated)
            if rp_dt.tzinfo is None:
                rp_dt = rp_dt.replace(
                    tzinfo=datetime.timezone(datetime.timedelta(hours=9))
                )
            if (now - rp_dt).total_seconds() > 86400:
                alerts.append("  [WARN] research_pool 24h以上未更新")
        except (ValueError, TypeError):
            pass

    if not alerts:
        lines.append("  None")
    else:
        lines.extend(alerts)

    lines.extend([
        "",
        "--- Safety ---",
        f"  Emergency Stop : {'ACTIVE' if emergency else 'inactive'}",
        f"  Circuit Breaker: {'ACTIVE' if cb_active else 'inactive'}",
        f"  Daily Posts    : {today_posts} / {max_daily_posts}",
        f"  Token Expires  : {token_days}",
    ])

    return "\n".join(lines)


def main() -> None:
    sm = StateManager()
    watch = "--watch" in sys.argv

    if watch:
        try:
            while True:
                # Clear screen (cross-platform)
                print("\033[2J\033[H", end="")
                print(render_dashboard(sm))
                print(f"\n(自動更新: 30秒ごと | Ctrl+C で終了)")
                time.sleep(30)
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
    else:
        print(render_dashboard(sm))


if __name__ == "__main__":
    main()
