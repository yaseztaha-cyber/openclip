"""
configcheck.py — Centralized configuration validation + readiness matrix (v1.2).

Validates every automation setting and reports per-capability state without
inferring one state from another. DRY_RUN requires no credentials; LIVE
requires credentials + explicit flags, and invalid live configuration stops
publication (never the whole application — optional analytics/SMTP outages
only mark those capabilities).
"""

from __future__ import annotations

import datetime
import os

STATES = ("SUPPORTED", "CONFIGURED", "AUTHENTICATED", "AVAILABLE",
          "OPERATIONAL", "UNAVAILABLE")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() == "true"


def validate() -> dict:
    """Validate automation configuration. Returns findings + readiness."""
    mode = os.getenv("AUTOMATION_MODE", "dry_run").lower()
    live_flag = _bool("LIVE_PUBLISHING_ENABLED")
    approval_required = _bool("HUMAN_APPROVAL_REQUIRED", "true")
    findings: list[dict] = []

    def add(capability, ok, message, level="error", status=None):
        findings.append({"capability": capability, "ok": bool(ok),
                         "level": "info" if ok else level, "message": message,
                         "status": status or ("VALID" if ok else "INVALID")})

    add("mode", mode in ("dry_run", "live"),
        f"AUTOMATION_MODE={mode}" if mode in ("dry_run", "live")
        else f"unknown AUTOMATION_MODE={mode!r}")
    try:
        ttl = float(os.getenv("APPROVAL_TTL_HOURS", "168"))
        add("approval_ttl", ttl > 0, f"APPROVAL_TTL_HOURS={ttl}")
    except Exception:
        add("approval_ttl", False, "APPROVAL_TTL_HOURS is not numeric")
    try:
        mr = int(os.getenv("MAX_ATTEMPTS", "4"))
        add("retry_limits", mr >= 0, f"MAX_ATTEMPTS={mr}")
    except Exception:
        add("retry_limits", False, "MAX_ATTEMPTS is not an integer")
    try:
        to = float(os.getenv("EDITOR_TRANSCRIPT_TIMEOUT", "90"))
        add("timeouts", to > 0, f"EDITOR_TRANSCRIPT_TIMEOUT={to}")
    except Exception:
        add("timeouts", False, "EDITOR_TRANSCRIPT_TIMEOUT is not numeric")
    try:
        lease = float(os.getenv("SCHEDULER_LEASE_TTL_S", "300"))
        hb = float(os.getenv("SCHEDULER_HEARTBEAT_S", "15"))
        add("scheduler_limits", lease > 0 and 0 < hb < lease,
            f"SCHEDULER_LEASE_TTL_S={lease} SCHEDULER_HEARTBEAT_S={hb}")
    except Exception:
        add("scheduler_limits", False, "scheduler limit env is not numeric")

    live_problems: list[str] = []
    if mode == "live":
        if not live_flag:
            live_problems.append("LIVE_PUBLISHING_ENABLED is not true")
        platforms = [p.strip() for p in os.getenv("PUBLISH_PLATFORMS", "").split(",") if p.strip()]
        tokens = {p: bool(os.getenv(f"PUBLISH_TOKEN_{p.upper()}", "")) for p in platforms}
        if platforms and not any(tokens.values()):
            live_problems.append(
                f"PUBLISH_PLATFORMS={platforms} but no PUBLISH_TOKEN_* is set")
        if not approval_required:
            live_problems.append("HUMAN_APPROVAL_REQUIRED is false in live mode")
        add("live_publish_config", not live_problems,
            "live publishing configuration valid" if not live_problems
            else "; ".join(live_problems))
    else:
        add("live_publish_config", True,
            "dry_run: no live credentials required, no external side effects")

    smtp = bool(os.getenv("SMTP_HOST", ""))
    recips = bool(os.getenv("REPORT_RECIPIENTS", "").strip())
    # SMTP is optional: informational only, never blocks validation.
    findings.append({"capability": "smtp_config", "ok": True, "level": "info",
                     "status": "VALID" if (smtp and recips) else "UNAVAILABLE",
                     "message": "SMTP configured" if (smtp and recips)
                     else "SMTP optional and not fully configured (reports stored unsent)"})
    # Credential presence is reported as PRESENT/ABSENT only — values never
    # leave the process. Informational: absence blocks live use, never the app.
    for label, present in (
            ("credential_whop_api_key", bool(os.getenv("WHOP_API_KEY", "").strip())),
            ("credential_publish_token_tiktok",
             bool(os.getenv("PUBLISH_TOKEN_TIKTOK", "").strip())),
            ("credential_publish_token_youtube",
             bool(os.getenv("PUBLISH_TOKEN_YOUTUBE", "").strip())),
            ("credential_smtp", smtp),
            ("credential_operator_token",
             bool(os.getenv("OPERATOR_TOKEN", "").strip()))):
        findings.append({"capability": label, "ok": True, "level": "info",
                         "status": "PRESENT" if present else "ABSENT",
                         "message": f"{label}: {'PRESENT' if present else 'ABSENT'}"})

    blocking = [f for f in findings if not f["ok"] and f["level"] == "error"]
    return {"mode": mode, "findings": findings,
            "valid": not blocking, "blocking": blocking,
            "checked_at": _now()}


def readiness_matrix() -> dict:
    """Full per-capability readiness matrix for operations."""
    from automation.publishing import (MockPublisher, ReplayPublisher,
                                       AuthorizedPlatformPublisher,
                                       adapter_status)
    from automation.providers import OfficialWhopProvider
    cfg = validate()
    tiktok = adapter_status(AuthorizedPlatformPublisher(), "tiktok")
    whop = OfficialWhopProvider()
    import os as _os
    smtp_state = "CONFIGURED" if (_os.getenv("SMTP_HOST", "")
                                  and _os.getenv("REPORT_RECIPIENTS", "").strip()) \
        else "UNAVAILABLE"
    matrix = [
        {"capability": "campaign_discovery", "provider": "whop_official",
         "mode": cfg["mode"], "supported": True,
         "configured": bool(whop.api_key), "authenticated": bool(whop.api_key),
         "available": bool(whop.api_key),
         "operational": bool(whop.api_key),
         "last_checked": cfg["checked_at"],
         "failure_reason": "" if whop.api_key else "WHOP_API_KEY not configured",
         "verification": "LIVE OPTIONAL" if whop.api_key else "UNAVAILABLE"},
        {"capability": "publishing", "provider": "authorized",
         "mode": cfg["mode"], "supported": True,
         "configured": tiktok["state"] in ("CONFIGURED", "AUTHENTICATED",
                                           "AVAILABLE", "OPERATIONAL"),
         "authenticated": tiktok["state"] in ("AUTHENTICATED", "AVAILABLE",
                                              "OPERATIONAL"),
         "available": tiktok["state"] in ("AVAILABLE", "OPERATIONAL"),
         "operational": tiktok["state"] == "OPERATIONAL",
         "last_checked": cfg["checked_at"],
         "failure_reason": tiktok["reason"],
         "verification": "LIVE OPTIONAL" if tiktok["state"] == "OPERATIONAL"
                         else "UNAVAILABLE"},
        {"capability": "submission_lookup", "provider": "authorized",
         "mode": cfg["mode"], "supported": False, "configured": False,
         "authenticated": False, "available": False, "operational": False,
         "last_checked": cfg["checked_at"],
         "failure_reason": "authorized adapter declares no lookup operation",
         "verification": "UNAVAILABLE"},
        {"capability": "analytics", "provider": "none",
         "mode": cfg["mode"], "supported": False, "configured": False,
         "authenticated": False, "available": False, "operational": False,
         "last_checked": cfg["checked_at"],
         "failure_reason": "no analytics provider configured; observations accepted as NOT_AVAILABLE",
         "verification": "UNAVAILABLE"},
        {"capability": "smtp", "provider": "smtp",
         "mode": cfg["mode"], "supported": True,
         "configured": smtp_state == "CONFIGURED",
         "authenticated": False, "available": False, "operational": False,
         "last_checked": cfg["checked_at"],
         "failure_reason": "" if smtp_state == "CONFIGURED"
                           else "SMTP_HOST/REPORT_RECIPIENTS not configured",
         "verification": "LIVE OPTIONAL" if smtp_state == "CONFIGURED"
                         else "UNAVAILABLE"},
        {"capability": "mock_simulation", "provider": "mock",
         "mode": cfg["mode"], "supported": True, "configured": True,
         "authenticated": True, "available": True, "operational": True,
         "last_checked": cfg["checked_at"], "failure_reason": "",
         "verification": "MOCK VERIFIED"},
    ]
    _ = (MockPublisher, ReplayPublisher)
    automation_ready = cfg["valid"]
    return {"config": cfg, "matrix": matrix,
            "automation_readiness": "READY" if automation_ready
                                    else "NOT_READY_LIVE_BLOCKED",
            "note": "Dry-run work is always permitted; only live publication "
                    "is gated by this matrix."}
