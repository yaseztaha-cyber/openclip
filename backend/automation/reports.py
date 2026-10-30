"""
reports.py — WeeklyReportService: aggregate → HTML + plaintext → persist → email.

Email uses SMTP from environment (SMTP_HOST/PORT/USER/PASS/FROM). If no
provider is configured, the report is stored with email_status
"EMAIL NOT SENT" — delivery is never pretended.
"""

from __future__ import annotations

import datetime
import logging
import os
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

EMAIL_DISABLED = "EMAIL_DISABLED"
EMAIL_NOT_CONFIGURED = "EMAIL_NOT_CONFIGURED"
EMAIL_SEND_STARTED = "EMAIL_SEND_STARTED"
EMAIL_SENT = "EMAIL_SENT"
EMAIL_FAILED = "EMAIL_FAILED"
EMAIL_UNKNOWN = "EMAIL_UNKNOWN"
# Legacy token (pre-v1.2 rows); new code never writes it.
EMAIL_NOT_SENT = "EMAIL NOT SENT"


def week_bounds(ref: datetime.date | None = None) -> tuple[str, str]:
    ref = ref or datetime.date.today()
    start = ref - datetime.timedelta(days=ref.weekday())
    end = start + datetime.timedelta(days=6)
    return start.isoformat(), end.isoformat()


def build_report_payload(week_start: str, week_end: str, campaigns: list[dict],
                         approvals: list[dict], publications: list[dict],
                         analytics_summary: dict, errors: list[str]) -> dict:
    import version as _version
    eligible = sum(1 for c in campaigns if c.get("status") == "ELIGIBLE")
    rejected = sum(1 for c in campaigns if c.get("status") == "REJECTED")
    review = sum(1 for c in campaigns if c.get("status") == "NEEDS_REVIEW")
    by_status: dict[str, int] = {}
    for a in approvals:
        by_status[a.get("status", "UNKNOWN")] = by_status.get(a.get("status", "UNKNOWN"), 0) + 1
    # Publication states are submission states, not analytics: UNKNOWN is
    # reported as unknown (never counted as confirmed), missing analytics
    # stays NOT_AVAILABLE (never zero).
    pub_states: dict[str, int] = {}
    for p in publications:
        st = (p.get("submission_state") or "NOT_SUBMITTED").upper()
        pub_states[st] = pub_states.get(st, 0) + 1
    unknown_pub = pub_states.get("UNKNOWN_EXTERNAL_RESULT", 0) + \
        pub_states.get("AWAITING_CONFIRMATION", 0) + pub_states.get("NEEDS_REVIEW", 0)
    failed_pub = pub_states.get("FAILED", 0)
    confirmed_pub = pub_states.get("CONFIRMED", 0)
    return {
        "week_start": week_start, "week_end": week_end,
        "application_version": _version.APP_VERSION,
        "schema_version": _version.SCHEMA_VERSION,
        "campaigns": {"eligible": eligible, "rejected": rejected,
                      "needs_review": review, "processed": len(campaigns)},
        "clips": {"analyzed": by_status.get("SCORED", 0) + by_status.get("SELECTED", 0)
                  + by_status.get("RENDERED", 0) + by_status.get("READY_FOR_APPROVAL", 0)
                  + by_status.get("APPROVED", 0) + by_status.get("PUBLISHED", 0),
                  "selected": by_status.get("SELECTED", 0),
                  "approved": by_status.get("APPROVED", 0),
                  "published": analytics_summary.get("published_count", 0)},
        "publications": {"confirmed": confirmed_pub,
                         "unknown_publication": unknown_pub,
                         "failed_publication": failed_pub,
                         "by_state": pub_states},
        "performance": analytics_summary,
        "errors": errors,
    }


def render_html(payload: dict, publications: list[dict]) -> str:
    p = payload["performance"]
    pubs = payload.get("publications", {})
    rows = "".join(
        f"<li>{pub.get('clip_id')}: "
        f"<a href='{pub.get('post_url', '')}'>Watch</a> "
        f"({pub.get('platform', '')} · {pub.get('submission_state', '')})</li>"
        for pub in publications[:50])
    return f"""<html><body style="font-family:sans-serif;max-width:640px">
<h2>AUTOCLIP WEEKLY REPORT</h2>
<p>Week: {payload['week_start']} – {payload['week_end']}</p>
<h3>CAMPAIGNS</h3>
<p>{payload['campaigns']['eligible']} eligible · {payload['campaigns']['rejected']} rejected ·
{payload['campaigns']['needs_review']} needs review · {payload['campaigns']['processed']} processed</p>
<h3>CLIPS</h3>
<p>{payload['clips']['analyzed']} analyzed · {payload['clips']['selected']} selected ·
{payload['clips']['approved']} approved · {payload['clips']['published']} published</p>
<h3>PUBLICATIONS</h3>
<p>Confirmed: {pubs.get('confirmed', 0)} · Unknown (awaiting reconciliation): {pubs.get('unknown_publication', 0)} ·
Failed: {pubs.get('failed_publication', 0)}</p>
<h3>PERFORMANCE</h3>
<p>Total views: {p.get('total_views')} · Engagement: {p.get('engagement')}</p>
<h3>REVENUE</h3>
<p>Confirmed: {p.get('confirmed_earnings')} · Pending: {p.get('pending_earnings')} ·
Estimated: {p.get('estimated_earnings')}</p>
<h3>PUBLISHED</h3><ul>{rows or '<li>none</li>'}</ul>
<h3>ISSUES</h3><p>{'; '.join(payload['errors']) or 'none'}</p>
</body></html>"""


def render_text(payload: dict, publications: list[dict]) -> str:
    p = payload["performance"]
    pubs = payload.get("publications", {})
    lines = [f"AUTOCLIP WEEKLY REPORT", f"Week: {payload['week_start']}–{payload['week_end']}", "",
             f"CAMPAIGNS: {payload['campaigns']}", f"CLIPS: {payload['clips']}", "",
             f"PUBLICATIONS confirmed={pubs.get('confirmed', 0)} "
             f"unknown={pubs.get('unknown_publication', 0)} "
             f"failed={pubs.get('failed_publication', 0)}", "",
             f"VIEWS: {p.get('total_views')} ENGAGEMENT: {p.get('engagement')}",
             f"CONFIRMED: {p.get('confirmed_earnings')} PENDING: {p.get('pending_earnings')}", ""]
    for pub in publications[:50]:
        lines.append(f"- {pub.get('clip_id')} {pub.get('post_url', '')}")
    if payload["errors"]:
        lines += ["", "ISSUES:"] + [f"- {e}" for e in payload["errors"]]
    return "\n".join(lines)


def send_email(subject: str, html: str, text: str,
               recipients: list[str] | None = None,
               allow_external: bool = False) -> str:
    """Explicit delivery states; EMAIL_SENT only when SMTP accepts.

    EMAIL_UNKNOWN covers partial acceptance (some recipients refused).
    Delivery identity (report row + content hash) is stored by the caller.
    """
    recipients = recipients or [r.strip() for r in os.getenv("REPORT_RECIPIENTS", "").split(",") if r.strip()]
    host = os.getenv("SMTP_HOST", "")
    # Dry-run safe: external delivery requires explicit live allowance.
    if not allow_external:
        logger.info("email suppressed (not live) → EMAIL_DISABLED")
        return EMAIL_DISABLED
    if not host or not recipients:
        logger.info("email provider unconfigured → EMAIL_NOT_CONFIGURED")
        return EMAIL_NOT_CONFIGURED
    logger.info("email send started → EMAIL_SEND_STARTED")
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = os.getenv("SMTP_FROM", "autoclip@localhost")
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
            if user:
                s.login(user, pw)
            refused = s.sendmail(msg["From"], recipients, msg.as_string())
        if refused:
            logger.warning("email partially refused → EMAIL_UNKNOWN")
            return EMAIL_UNKNOWN
        return EMAIL_SENT
    except Exception as exc:
        logger.warning("weekly email failed → EMAIL_FAILED: %s", type(exc).__name__)
        return EMAIL_FAILED


def new_report_id() -> str:
    return f"rep_{uuid.uuid4().hex[:10]}"


def report_hash(payload: dict) -> str:
    import hashlib
    import json as _json
    blob = _json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, default=str)
    return "rph_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
