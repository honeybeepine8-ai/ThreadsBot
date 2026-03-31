"""Morning review — daily Gmail digest & reply-based approval.

Flow:
  1. Scheduler calls send_morning_email() at 07:00 (cron).
     - Fetches all pending drafts from draft_queue.json.
     - Sends a formatted email listing each draft.
     - Saves Message-ID + date to system_state.json["morning_review"].

  2. Scheduler calls check_approval_reply() every 10 minutes (interval).
     - Polls Gmail IMAP INBOX for a reply containing "OK".
     - On detection: moves all pending_review drafts to post_queue.json.
     - Poster then distributes them throughout the day automatically.

Configuration (config/settings.yaml → morning_review):
    send_time: "07:00"
    approval_keyword: "OK"
    check_interval_minutes: 10

Environment variables:
    GMAIL_USER          — sender address (bot's Gmail account)
    GMAIL_APP_PASSWORD  — 16-char Google App Password
    GMAIL_TO            — recipient address (defaults to GMAIL_USER)
"""

from __future__ import annotations

import datetime
import email as email_lib
import imaplib
import os
import smtplib
import uuid
from email.header import decode_header
from email.mime.text import MIMEText
from typing import Any

import yaml

from core.logger import get_logger
from core.state_manager import StateManager

logger = get_logger("morning_review")

_DRAFT_QUEUE_FILE = "draft_queue.json"
_POST_QUEUE_FILE = "post_queue.json"
_SYSTEM_STATE_FILE = "system_state.json"

JST = datetime.timezone(datetime.timedelta(hours=9))

_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "settings.yaml",
)


def _load_config() -> dict[str, Any]:
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("morning_review", {})
    except (OSError, yaml.YAMLError):
        return {}


class MorningReview:
    """Send daily draft summary email and approve on "OK" reply."""

    def __init__(self) -> None:
        self.gmail_user: str = os.environ.get("GMAIL_USER", "")
        self.gmail_password: str = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.gmail_to: str = os.environ.get("GMAIL_TO", self.gmail_user)
        self.enabled: bool = bool(self.gmail_user and self.gmail_password)
        self.state = StateManager()

        cfg = _load_config()
        self.approval_keyword: str = cfg.get("approval_keyword", "OK").upper()

        if not self.enabled:
            logger.info(
                "MorningReview disabled: GMAIL_USER or GMAIL_APP_PASSWORD not set."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_morning_email(self) -> bool:
        """Send the morning review email with all pending drafts.

        Idempotent: skips if an email was already sent today.

        Returns:
            True if the email was sent (or was already sent today).
        """
        if not self.enabled:
            return False

        today = datetime.date.today().isoformat()
        mr_state = self._load_mr_state()

        if mr_state.get("date") == today and mr_state.get("email_sent"):
            logger.info("Morning review email already sent today (%s). Skipping.", today)
            return True

        draft_data = self.state.load_json(_DRAFT_QUEUE_FILE)
        pending = [
            d for d in draft_data.get("drafts", [])
            if d.get("status") == "pending_review"
        ]

        if not pending:
            logger.info("No pending drafts — morning review email skipped.")
            mr_state.update({"date": today, "email_sent": False, "approved": False})
            self._save_mr_state(mr_state)
            return True

        subject = f"[ThreadsBot] 朝レビュー {today} ({len(pending)}件)"
        body = self._build_email_body(pending, today)
        message_id = f"<threadbot-{today}-{uuid.uuid4().hex[:8]}@review>"

        success = self._send_email(subject, body, message_id)
        if success:
            mr_state.update({
                "date": today,
                "email_sent": True,
                "message_id": message_id,
                "subject": subject,
                "approved": False,
                "pending_count": len(pending),
            })
            self._save_mr_state(mr_state)
            logger.info(
                "Morning review email sent: %d drafts, subject='%s'",
                len(pending), subject,
            )
        return success

    def check_approval_reply(self) -> bool:
        """Poll Gmail IMAP for an approval reply.

        Returns:
            True if approval was detected and drafts were moved to post_queue.
        """
        if not self.enabled:
            return False

        today = datetime.date.today().isoformat()
        mr_state = self._load_mr_state()

        if mr_state.get("date") != today or not mr_state.get("email_sent"):
            return False

        if mr_state.get("approved"):
            logger.debug("Already approved today — skipping IMAP check.")
            return True

        original_subject = mr_state.get("subject", "")
        if not original_subject:
            return False

        reply_subject = f"Re: {original_subject}"
        found = self._poll_imap_for_reply(reply_subject)
        if not found:
            return False

        count = self._approve_all_pending()
        mr_state.update({
            "approved": True,
            "approved_count": count,
            "approved_at": datetime.datetime.now(JST).isoformat(),
        })
        self._save_mr_state(mr_state)
        logger.info(
            "Morning review approved via email reply: %d drafts moved to post_queue.",
            count,
        )
        return True

    # ------------------------------------------------------------------
    # Email composition
    # ------------------------------------------------------------------

    @staticmethod
    def _build_email_body(pending: list[dict[str, Any]], today: str) -> str:
        lines = [
            f"■ ThreadsBot 朝レビュー — {today}",
            f"今日の投稿候補: {len(pending)} 件",
            "",
            "【承認】このメールに「OK」とだけ返信 → 全件承認・自動投稿開始",
            "【見送り】返信しない → 今日は投稿なし",
            "",
            "━" * 36,
        ]

        for i, draft in enumerate(pending, 1):
            score = draft.get("quality_score", "?")
            pattern = draft.get("pattern", "?")
            category = draft.get("category", "?")
            content = draft.get("content", "")

            lines += [
                "",
                f"【{i}】 スコア {score}  |  {pattern}  |  {category}",
                "─" * 30,
                content,
            ]
            for j, tp in enumerate(draft.get("thread_posts") or [], 2):
                lines.append(f"  ↳ [{j}] {tp}")

        lines += ["", "━" * 36]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # SMTP sending
    # ------------------------------------------------------------------

    def _send_email(self, subject: str, body: str, message_id: str) -> bool:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.gmail_user
        msg["To"] = self.gmail_to
        msg["Message-ID"] = message_id

        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(self.gmail_user, self.gmail_password)
                server.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("Failed to send morning review email: %s", exc)
            return False

    # ------------------------------------------------------------------
    # IMAP polling
    # ------------------------------------------------------------------

    def _poll_imap_for_reply(self, reply_subject: str) -> bool:
        """Search INBOX for today's emails, check for approval reply.

        Searches all messages received today, matches subject, then checks
        the first non-quoted line of the body for the approval keyword.
        """
        since_str = datetime.date.today().strftime("%d-%b-%Y")
        try:
            with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
                imap.login(self.gmail_user, self.gmail_password)
                imap.select("INBOX")

                _, msg_nums = imap.search(None, f"SINCE {since_str}")
                if not msg_nums or not msg_nums[0]:
                    logger.debug("No messages in INBOX since %s.", since_str)
                    return False

                for num in msg_nums[0].split():
                    _, data = imap.fetch(num, "(RFC822)")
                    if not data or not data[0]:
                        continue
                    raw = data[0][1]
                    msg = email_lib.message_from_bytes(raw)

                    subj = _decode_mime_header(msg.get("Subject", ""))
                    if reply_subject.lower() not in subj.lower():
                        continue

                    body = _extract_text_body(msg)
                    if self._is_approval(body):
                        logger.info("Approval reply found (subject: %s).", subj)
                        return True

        except (imaplib.IMAP4.error, OSError) as exc:
            logger.error("IMAP error while checking for reply: %s", exc)

        return False

    def _is_approval(self, body: str) -> bool:
        """Return True if the first meaningful line contains the approval keyword."""
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(">"):
                continue
            return self.approval_keyword in stripped.upper()
        return False

    # ------------------------------------------------------------------
    # Draft approval
    # ------------------------------------------------------------------

    def _approve_all_pending(self) -> int:
        """Move all pending_review drafts to post_queue. Returns count."""
        now_str = datetime.datetime.now(JST).isoformat()
        draft_data = self.state.load_json(_DRAFT_QUEUE_FILE)
        queue_data = self.state.load_json(_POST_QUEUE_FILE)

        existing_ids = {p["id"] for p in queue_data.get("queue", [])}
        count = 0

        for draft in draft_data.get("drafts", []):
            if draft.get("status") != "pending_review":
                continue
            draft["status"] = "approved"
            draft["review"] = {
                "action": "morning_review_approved",
                "reviewed_at": now_str,
            }
            post_item = _build_post_item(draft)
            if post_item["id"] not in existing_ids:
                queue_data.setdefault("queue", []).append(post_item)
                existing_ids.add(post_item["id"])
                count += 1

        draft_data["last_updated"] = now_str
        queue_data["last_updated"] = now_str
        self.state.save_json(_DRAFT_QUEUE_FILE, draft_data)
        self.state.save_json(_POST_QUEUE_FILE, queue_data)
        return count

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_mr_state(self) -> dict[str, Any]:
        system_state = self.state.load_json(_SYSTEM_STATE_FILE)
        return system_state.get("morning_review", {})

    def _save_mr_state(self, mr_state: dict[str, Any]) -> None:
        system_state = self.state.load_json(_SYSTEM_STATE_FILE)
        system_state["morning_review"] = mr_state
        self.state.save_json(_SYSTEM_STATE_FILE, system_state)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _decode_mime_header(raw: str) -> str:
    """Decode a MIME-encoded email header value to a plain string."""
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_text_body(msg: email_lib.message.Message) -> str:
    """Extract the plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


def _build_post_item(draft: dict[str, Any]) -> dict[str, Any]:
    """Convert a draft dict into a post_queue-compatible item."""
    skip_keys = {"flags", "expires_at", "review", "original_content", "post_queue_id"}
    post_item = {k: v for k, v in draft.items() if k not in skip_keys}
    post_item["id"] = draft.get("post_queue_id", draft["id"].replace("d_", "q_", 1))
    post_item["status"] = "pending"
    return post_item
