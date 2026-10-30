"""
events.py — Structured observability event log (Phase 23).

Every major action emits a JSON event with event_type / timestamp / job_id /
campaign_id / clip_id / render_id / actor / mode / status / duration_ms.
Secrets are never logged (values that look like tokens are redacted).
"""

from __future__ import annotations

import datetime
import json
import logging
import re

logger = logging.getLogger("automation.events")

_SECRET_HINTS = re.compile(r"(token|secret|password|api[_-]?key|authorization|cookie|session)", re.I)


def _redact(value):
    if isinstance(value, str) and len(value) > 8 and _SECRET_HINTS.search(value):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if _SECRET_HINTS.search(str(k)) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def emit(event_type: str, *, status: str = "ok", actor: str = "automation",
         mode: str | None = None, job_id=None, campaign_id=None, clip_id=None,
         render_id=None, duration_ms=None, details=None) -> dict:
    import os
    event = {
        "event_type": event_type,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "actor": actor,
        "mode": mode or os.getenv("AUTOMATION_MODE", "dry_run"),
        "status": status,
        "job_id": job_id,
        "campaign_id": campaign_id,
        "clip_id": clip_id,
        "render_id": render_id,
        "duration_ms": duration_ms,
        "details": _redact(details or {}),
    }
    logger.info("automation_event %s", json.dumps(event, default=str))
    return event
