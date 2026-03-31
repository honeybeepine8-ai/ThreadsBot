"""Check Threads post views/impressions.

Usage::

    python scripts/check_views.py            # show cached metrics
    python scripts/check_views.py --refresh  # fetch latest from Threads API
    python scripts/check_views.py --top 5    # show top N posts by views
"""

from __future__ import annotations

import datetime
import io
import sys
from pathlib import Path

# Windows端末でUTF-8出力を強制
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.state_manager import StateManager
from core.constants import JST


def _fmt_time(iso_str: str | None) -> str:
    if not iso_str:
        return "---"
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
        return dt.strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return "---"


def _truncate(text: str, n: int = 30) -> str:
    text = text.replace("\n", " ")
    return text[:n] + "…" if len(text) > n else text


def show_table(posts: list[dict], top: int | None = None) -> None:
    # Sort by views descending
    sorted_posts = sorted(
        posts,
        key=lambda p: p.get("metrics", {}).get("views", 0),
        reverse=True,
    )
    if top:
        sorted_posts = sorted_posts[:top]

    header = f"{'投稿日時':>12}  {'views':>6}  {'likes':>5}  {'replies':>7}  {'reposts':>7}  {'eng%':>5}  {'stage':>5}  {'内容'}"
    print(header)
    print("-" * len(header))

    for post in sorted_posts:
        m = post.get("metrics", {})
        posted = _fmt_time(post.get("posted_at"))
        views = m.get("views", 0)
        likes = m.get("likes", 0)
        replies = m.get("replies", 0)
        reposts = m.get("reposts", 0)
        eng = m.get("engagement_rate", 0.0)
        stage = m.get("current_stage") or ("unfetchable" if m.get("fetch_status") == "unfetchable" else "---")
        snippet = _truncate(post.get("content", ""), 30)
        print(f"{posted:>12}  {views:>6}  {likes:>5}  {replies:>7}  {reposts:>7}  {eng:>5.1f}  {stage:>5}  {snippet}")


def refresh_metrics(posts: list[dict], history_data: dict, sm: StateManager) -> int:
    """Fetch latest metrics from Threads API for posts not yet at terminal stage."""
    from services.threads_api import ThreadsAPIClient, ThreadsAPIError, RateLimitError, AuthenticationError

    now = datetime.datetime.now(JST)
    client = ThreadsAPIClient()
    updated = 0

    for post in posts:
        m = post.get("metrics", {})
        if m.get("fetch_status") == "unfetchable":
            continue
        if m.get("current_stage") == "24h":
            continue  # terminal stage, skip

        media_id = post.get("threads_media_id", "")
        if not media_id:
            continue

        try:
            insights = client.get_post_insights(media_id)
        except AuthenticationError as exc:
            print(f"[AUTH ERROR] {exc}", file=sys.stderr)
            break
        except RateLimitError as exc:
            print(f"[RATE LIMIT] {exc} — stopped early", file=sys.stderr)
            break
        except ThreadsAPIError as exc:
            error_msg = str(exc)
            fail_count = m.get("fetch_fail_count", 0) + 1
            m["fetch_fail_count"] = fail_count
            is_permanent = any(
                kw in error_msg
                for kw in ("does not exist", "HTTP 404", "cannot be loaded")
            )
            if is_permanent or fail_count >= 3:
                m["fetch_status"] = "unfetchable"
                m["fetch_error_reason"] = "deleted_or_inaccessible" if is_permanent else "max_failures"
                m["fetch_error_at"] = now.isoformat()
            print(f"  [SKIP] {post.get('id','?')}: {exc}", file=sys.stderr)
            continue

        total_eng = (
            insights.get("likes", 0)
            + insights.get("replies", 0)
            + insights.get("reposts", 0)
            + insights.get("quotes", 0)
        )
        views = insights.get("views", 0)
        for k in ("views", "likes", "replies", "reposts", "quotes"):
            m[k] = insights.get(k, 0)
        m["engagement_rate"] = round((total_eng / views * 100) if views else 0.0, 2)
        m["last_fetched"] = now.isoformat()
        m.pop("fetch_fail_count", None)
        updated += 1

    if updated:
        history_data["last_updated"] = now.isoformat()
        sm.save_json("post_history.json", history_data)

    client.close()
    return updated


def main() -> None:
    args = sys.argv[1:]
    do_refresh = "--refresh" in args
    top: int | None = None
    if "--top" in args:
        idx = args.index("--top")
        try:
            top = int(args[idx + 1])
        except (IndexError, ValueError):
            print("--top には数値を指定してください", file=sys.stderr)
            sys.exit(1)

    sm = StateManager()
    history_data = sm.load_json("post_history.json")
    posts: list[dict] = history_data.get("posts", [])

    if not posts:
        print("投稿履歴がありません。")
        return

    if do_refresh:
        print("Threads APIから最新メトリクスを取得中…")
        updated = refresh_metrics(posts, history_data, sm)
        print(f"更新完了: {updated}件\n")

    last_fetched_any = max(
        (p.get("metrics", {}).get("last_fetched") or "" for p in posts),
        default="",
    )
    print(f"投稿数: {len(posts)}件  |  最終API取得: {_fmt_time(last_fetched_any)}\n")
    show_table(posts, top=top)


if __name__ == "__main__":
    main()
