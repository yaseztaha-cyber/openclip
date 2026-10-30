"""
alerts.py — Local operational alerts from actual events (v1.2 §20).

Rules fire only on observed evidence (attempt rows, job rows, heartbeats,
telemetry, disk). Normal unavailability of optional services never alerts.
"""

from __future__ import annotations

import datetime
import time

FAILURE_BURST_N = 5
STALE_HEARTBEAT_S = 600.0


def _iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


async def evaluate(store, telemetry_summary=None, tmp_dir: str = "") -> list[dict]:
    alerts: list[dict] = []
    now = time.time()

    try:
        pubs = await store.list_publications()
        jobs = await store.list_jobs(200)
        reports = await store.list_reports()
        transitions = await store.list_transitions(limit=2000)
    except Exception as exc:
        # The store itself is unreadable: this is the one DB alert, and it
        # carries no query text or connection details.
        return [{"alert": "database_failure", "severity": "critical",
                 "detail": f"automation store unreadable: {type(exc).__name__}"}]

    unknown = [p for p in pubs if (p.get("submission_state") in
                                   ("UNKNOWN_EXTERNAL_RESULT", "AWAITING_CONFIRMATION",
                                    "NEEDS_REVIEW"))]
    if unknown:
        alerts.append({"alert": "submission_unknown", "severity": "warning",
                       "count": len(unknown),
                       "detail": f"{len(unknown)} submissions need reconciliation"})
    failed = [p for p in pubs if p.get("submission_state") == "FAILED"]
    recent_failed = [p for p in failed
                     if (now - (_iso(p.get("published_at")) or 0)) < 3600]
    if len(recent_failed) >= FAILURE_BURST_N:
        alerts.append({"alert": "repeated_provider_failures", "severity": "critical",
                       "count": len(recent_failed),
                       "detail": f"{len(recent_failed)} submission failures in the last hour"})

    running = [j for j in jobs if j.get("status") == "running"]
    stalled = [j for j in running
               if (now - (_iso(j.get("heartbeat_at")) or 0)) > STALE_HEARTBEAT_S]
    if stalled:
        alerts.append({"alert": "scheduler_stalled", "severity": "critical",
                       "count": len(stalled),
                       "detail": "jobs running with stale heartbeats"})
    exhausted = [j for j in jobs
                 if j.get("status") == "failed" and int(j.get("attempt", 0) or 0) >= 2]
    if exhausted:
        alerts.append({"alert": "job_retry_exhaustion", "severity": "warning",
                       "count": len(exhausted),
                       "detail": "jobs failed after retries"})

    # Render failure bursts: FAILED clip transitions in the last hour.
    render_failed = [t for t in transitions
                     if t.get("machine") == "clip" and t.get("new_state") == "FAILED"
                     and (now - (_iso(t.get("ts")) or 0)) < 3600]
    if len(render_failed) >= FAILURE_BURST_N:
        alerts.append({"alert": "render_failure_burst", "severity": "warning",
                       "count": len(render_failed),
                       "detail": f"{len(render_failed)} clip failures in the last hour"})

    # SMTP failures: only actual failed sends alert (unconfigured SMTP is
    # an expected state, never an alert).
    smtp_failed = [r for r in reports
                   if (r.get("email_status") or "") == "EMAIL_FAILED"]
    if smtp_failed:
        alerts.append({"alert": "smtp_failure", "severity": "warning",
                       "count": len(smtp_failed),
                       "detail": f"{len(smtp_failed)} report emails failed to send"})

    if tmp_dir:
        from automation.resources import disk_free_bytes, MIN_FREE_BYTES
        free = disk_free_bytes(tmp_dir)
        if 0 <= free < MIN_FREE_BYTES:
            alerts.append({"alert": "disk_exhaustion", "severity": "critical",
                           "free_bytes": free,
                           "detail": "render storage below minimum"})
    # NOTE: optional provider/SMTP unavailability and disabled live mode
    # intentionally never alert (expected states, not failures).
    return alerts
