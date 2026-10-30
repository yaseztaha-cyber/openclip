"""
logging_utils.py — Consistent structured logging with central redaction (v1.4).

All automation logging flows through here. Secret-shaped values are
redacted in ONE place (never scattered custom masking). Correlation IDs
(request_id → job_id → submission_attempt_id) are linkable via context
without exposing sensitive data.
"""

from __future__ import annotations

import contextvars
import logging
import time

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "automation_request_id", default="-")
job_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "automation_job_id", default="-")

_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey",
                 "authorization", "cookie", "session", "passwd", "private_key")


def redact(value):
    if isinstance(value, str):
        low = value.lower()
        if any(h in low for h in _SECRET_HINTS) and len(value) > 12:
            return "***REDACTED***"
        return value
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if any(h in str(k).lower() for h in _SECRET_HINTS)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f"automation.{component}")


def log(component: str, event: str, level: str = "info", *,
        job_id=None, campaign_id=None, clip_id=None, render_id=None,
        submission_attempt_id=None, duration_ms=None, status: str = "",
        details=None) -> dict:
    """Emit one structured record. Returns the record (never secrets)."""
    logger = get_logger(component)
    record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "level": level.upper(), "component": component, "event": event,
              "request_id": request_id_ctx.get(), "job_id": job_id or job_id_ctx.get(),
              "campaign_id": campaign_id, "clip_id": clip_id,
              "render_id": render_id,
              "submission_attempt_id": submission_attempt_id,
              "duration_ms": duration_ms, "status": status,
              "details": redact(details or {})}
    getattr(logger, level, logger.info)(
        "%s %s status=%s %s", component, event, status or "-",
        {k: v for k, v in record.items()
         if k not in ("timestamp", "level", "component", "event", "status")
         and v not in (None, "-", "")})
    return record
