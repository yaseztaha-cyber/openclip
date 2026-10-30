"""
qualification.py — Provider qualification records (v1.3).

Every capability carries exactly one evidence state:
  MOCK VERIFIED | REPLAY VERIFIED | LIVE OPTIONAL | LIVE VERIFIED |
  UNAVAILABLE | UNSUPPORTED
plus per-capability verification (SUPPORTED/VERIFIED/UNVERIFIED/UNAVAILABLE).

LIVE_OPTIONAL is never upgraded to LIVE_VERIFIED without an actual
successful authorized test. Qualification records persist in
automation_provider_qualifications; history is append-only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid

MOCK_VERIFIED = "MOCK VERIFIED"
REPLAY_VERIFIED = "REPLAY VERIFIED"
LIVE_OPTIONAL = "LIVE OPTIONAL"
LIVE_VERIFIED = "LIVE VERIFIED"
UNAVAILABLE = "UNAVAILABLE"
UNSUPPORTED = "UNSUPPORTED"

EVIDENCE_STATES = frozenset({MOCK_VERIFIED, REPLAY_VERIFIED, LIVE_OPTIONAL,
                             LIVE_VERIFIED, UNAVAILABLE, UNSUPPORTED})

CAPABILITIES = ("SUBMIT", "LOOKUP", "STATUS", "ANALYTICS", "HEALTH",
                "IDEMPOTENCY", "RETRY_AFTER")

CAP_VERIFICATION = frozenset({"SUPPORTED", "VERIFIED", "UNVERIFIED",
                              "UNAVAILABLE"})


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_qualification(*, provider: str, platform: str,
                      adapter_version: str = "v1",
                      client_version: str = "v1",
                      capabilities: dict | None = None,
                      verification_type: str = UNAVAILABLE,
                      verification_scope: str = "",
                      limitations: str = "",
                      required_credentials: list | None = None,
                      checks: dict | None = None) -> dict:
    """Build a qualification record. Rejects unknown evidence states."""
    if verification_type not in EVIDENCE_STATES:
        raise ValueError(f"unknown evidence state: {verification_type}")
    caps = {}
    for cap in CAPABILITIES:
        state = (capabilities or {}).get(cap, "UNAVAILABLE")
        if state not in CAP_VERIFICATION:
            raise ValueError(f"unknown capability state for {cap}: {state}")
        caps[cap] = state
    body = {"provider": provider, "platform": platform, "caps": caps,
            "verification_type": verification_type}
    qid = "qual_" + hashlib.sha256(
        json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return {
        "qualification_id": qid,
        "record_id": f"qr_{uuid.uuid4().hex[:10]}",
        "provider": provider,
        "platform": platform,
        "adapter_version": adapter_version,
        "client_version": client_version,
        "capabilities": caps,
        "configuration_status": (checks or {}).get("configuration_status", "UNKNOWN"),
        "authentication_status": (checks or {}).get("authentication_status", "UNKNOWN"),
        "availability_status": (checks or {}).get("availability_status", "UNKNOWN"),
        "operational_status": (checks or {}).get("operational_status", "UNKNOWN"),
        "last_checked": _now(),
        "verification_type": verification_type,
        "verification_scope": verification_scope,
        "limitations": limitations,
        "required_credentials": required_credentials or [],
        "checks": checks or {},
    }


def upgrade_to_live_verified(record: dict, *, scope: str,
                              evidence: dict) -> dict:
    """Upgrade LIVE_OPTIONAL → LIVE_VERIFIED only with actual evidence."""
    if record["verification_type"] != LIVE_OPTIONAL:
        raise ValueError("only LIVE_OPTIONAL records can be upgraded")
    if not evidence.get("authorized") or not evidence.get("succeeded"):
        raise ValueError("LIVE_VERIFIED requires a successful authorized test")
    out = dict(record)
    out["verification_type"] = LIVE_VERIFIED
    out["verification_scope"] = scope
    out["last_checked"] = _now()
    out["record_id"] = f"qr_{uuid.uuid4().hex[:10]}"
    return out
