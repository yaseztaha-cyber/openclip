"""
submissions.py — Provider submission reliability (v1.1).

Core distinction: INTERNAL idempotency (exactly-once CLAIM semantics inside
this database) is NOT the same as EXTERNAL exactly-once delivery (which can
only be claimed when the provider explicitly guarantees it). A timeout,
connection reset, or unparsable response AFTER request transmission means
the platform may still have accepted the request — that is
UNKNOWN_EXTERNAL_RESULT, never FAILED, never an automatic retry.
"""

from __future__ import annotations

import logging
import random
import uuid

logger = logging.getLogger(__name__)

# --- Submission states (extended machine; see statemachine.py) ---
NOT_SUBMITTED = "NOT_SUBMITTED"
CLAIMED = "CLAIMED"
SUBMISSION_STARTED = "SUBMISSION_STARTED"
SUBMISSION_ACCEPTED = "SUBMISSION_ACCEPTED"
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
CONFIRMED = "CONFIRMED"
FAILED = "FAILED"
UNKNOWN_EXTERNAL_RESULT = "UNKNOWN_EXTERNAL_RESULT"
NEEDS_REVIEW = "NEEDS_REVIEW"
ALREADY_PUBLISHED = "ALREADY_PUBLISHED"

UNRESOLVED_STATES = frozenset({UNKNOWN_EXTERNAL_RESULT, AWAITING_CONFIRMATION,
                               NEEDS_REVIEW})

# --- Error classes ---
NON_RETRYABLE = "NON_RETRYABLE"
RETRYABLE = "RETRYABLE"
AMBIGUOUS = "AMBIGUOUS"

_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 409, 422}
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Backoff bounds (seconds). Honors provider Retry-After when present.
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 300.0
MAX_ATTEMPTS = 4


def classify_provider_error(status_code=None, *, error_kind: str = "",
                            body: str = "") -> dict:
    """
    Map a provider outcome to NON_RETRYABLE | RETRYABLE | AMBIGUOUS.
    Ambiguous transport failures (timeout/reset/lost response AFTER the
    request may have been transmitted) are AMBIGUOUS, never FAILED.
    """
    kind = (error_kind or "").lower()
    if kind in ("timeout_after_transmission", "connection_reset",
                "response_lost", "malformed_response_after_submit",
                "timeout", "connection_error"):
        # Without proof the request never left, assume it may have.
        return {"class": AMBIGUOUS, "result_state": UNKNOWN_EXTERNAL_RESULT,
                "reason": f"ambiguous transport outcome: {kind or 'unknown'}"}
    if kind in ("request_never_sent", "validation_error", "policy_block",
                "auth_error", "not_found_invalid", "conflict_existing"):
        return {"class": NON_RETRYABLE, "result_state": FAILED,
                "reason": f"request never reached provider state: {kind}"}
    if status_code in _NON_RETRYABLE_STATUS:
        if status_code == 409:
            return {"class": NON_RETRYABLE, "result_state": ALREADY_PUBLISHED,
                    "reason": "provider reports an existing submission"}
        return {"class": NON_RETRYABLE, "result_state": FAILED,
                "reason": f"provider rejected with HTTP {status_code}"}
    if status_code in _RETRYABLE_STATUS:
        return {"class": RETRYABLE, "result_state": FAILED,
                "reason": f"provider transient HTTP {status_code}",
                "retryable": True}
    if status_code is None:
        return {"class": AMBIGUOUS, "result_state": UNKNOWN_EXTERNAL_RESULT,
                "reason": "no provider response observed"}
    return {"class": NON_RETRYABLE, "result_state": FAILED,
            "reason": f"unclassified provider HTTP {status_code}"}


def retry_delay_s(attempt: int, retry_after: float | None = None,
                  rng: random.Random | None = None) -> float:
    """Bounded exponential backoff honoring Retry-After; jittered."""
    if retry_after is not None:
        try:
            return max(0.0, min(float(retry_after), BACKOFF_MAX_S))
        except Exception:
            pass
    base = min(BACKOFF_BASE_S * (2 ** max(int(attempt) - 1, 0)), BACKOFF_MAX_S)
    jitter = (rng or random).uniform(0, base * 0.25)
    return round(min(base + jitter, BACKOFF_MAX_S), 3)


def new_attempt_id() -> str:
    return f"sub_{uuid.uuid4().hex[:12]}"


def redact_metadata(metadata: dict | None) -> dict:
    """Strip anything secret-shaped from provider metadata before storage."""
    import re
    clean: dict = {}
    for k, v in (metadata or {}).items():
        kl = str(k).lower()
        if any(h in kl for h in ("token", "secret", "password", "api_key",
                                 "apikey", "authorization", "cookie", "session")):
            clean[k] = "***REDACTED***"
        elif isinstance(v, str) and len(v) > 200:
            clean[k] = v[:200] + "…[truncated]"
        else:
            clean[k] = v
    return clean


class LookupUnavailable(Exception):
    """Provider exposes no submission-lookup operation."""


async def reconcile_one(store, lookup_fn, idempotency_key: str,
                        actor: str = "automation") -> dict:
    """
    Reconcile one unresolved submission against provider evidence.
    Returns {outcome} ∈ CONFIRMED | NOT_FOUND | FAILED | STILL_UNKNOWN |
    NEEDS_REVIEW. Never fabricates confirmation; preserves every attempt;
    never silently resets UNKNOWN to FAILED.
    """
    from . import events as _events
    pub = await store.find_publication_by_key(idempotency_key)
    if pub is None:
        return {"outcome": "NOT_FOUND", "reason": "no local submission record"}
    _events.emit("SUBMISSION_RECONCILIATION_STARTED", actor=actor,
                 campaign_id=pub.get("campaign_id"), clip_id=pub.get("clip_id"),
                 details={"idempotency_key": idempotency_key,
                          "from_state": pub.get("submission_state")})
    attempts = await store.list_submission_attempts(idempotency_key)
    provider_ids = [a.get("provider_submission_id") for a in attempts
                    if a.get("provider_submission_id")]
    try:
        found = await lookup_fn(idempotency_key=idempotency_key,
                                provider_submission_id=provider_ids[0]
                                if provider_ids else None)
    except LookupUnavailable:
        _events.emit("SUBMISSION_RECONCILED", actor=actor, status="still_unknown",
                     campaign_id=pub.get("campaign_id"), clip_id=pub.get("clip_id"),
                     details={"idempotency_key": idempotency_key,
                              "outcome": "STILL_UNKNOWN"})
        return {"outcome": "STILL_UNKNOWN",
                "reason": "provider exposes no submission lookup"}
    except Exception as exc:
        logger.warning("reconciliation lookup failed for %s: %s", idempotency_key, exc)
        return {"outcome": "STILL_UNKNOWN",
                "reason": f"lookup errored without evidence: {type(exc).__name__}"}
    if found is None:
        # Provider evidence: no such submission → a new controlled
        # submission is permitted (fresh attempt number continues).
        await store.update_publication_by_key(
            idempotency_key, submission_state=FAILED,
            submission_status="reconcile_not_found_retry_permitted")
        await store.log_decision(
            clip_id=pub.get("clip_id"), campaign_id=pub.get("campaign_id"),
            decision="submission_not_found",
            rules=["reconciliation"],
            evidence=[{"idempotency_key": idempotency_key,
                       "provider_ids_checked": provider_ids}],
            operator=actor)
        _events.emit("SUBMISSION_NOT_FOUND", actor=actor,
                     campaign_id=pub.get("campaign_id"), clip_id=pub.get("clip_id"),
                     details={"idempotency_key": idempotency_key})
        return {"outcome": "NOT_FOUND",
                "reason": "provider has no record; controlled retry permitted"}
    status = str((found or {}).get("status", "")).upper()
    post_url = (found or {}).get("post_url") or (found or {}).get("url")
    if status in ("CONFIRMED", "PUBLISHED", "LIVE", "COMPLETE", "COMPLETED"):
        if not post_url:
            await store.update_publication_by_key(idempotency_key,
                                                  submission_state=NEEDS_REVIEW,
                                                  submission_status="confirmed_without_url")
            return {"outcome": "NEEDS_REVIEW",
                    "reason": "provider confirms without a post URL; human check needed"}
        await store.update_publication_by_key(
            idempotency_key, submission_state=CONFIRMED, status="published",
            post_url=post_url, submission_status="reconciled_confirmed")
        await store.log_decision(
            clip_id=pub.get("clip_id"), campaign_id=pub.get("campaign_id"),
            decision="published", rules=["reconciliation"],
            evidence=[{"post_url": post_url, "provider_ids": provider_ids}],
            operator=actor)
        _events.emit("SUBMISSION_CONFIRMED", actor=actor,
                     campaign_id=pub.get("campaign_id"), clip_id=pub.get("clip_id"),
                     details={"idempotency_key": idempotency_key,
                              "post_url": post_url})
        return {"outcome": "CONFIRMED", "post_url": post_url}
    if status in ("FAILED", "REJECTED", "ERROR"):
        await store.update_publication_by_key(idempotency_key,
                                              submission_state=FAILED,
                                              submission_status="reconciled_failed")
        return {"outcome": "FAILED",
                "reason": f"provider reports failure: {status}"}
    return {"outcome": "STILL_UNKNOWN",
            "reason": f"provider state {status or 'unrecognized'} is inconclusive"}


async def find_unresolved(store, limit: int = 200) -> list[dict]:
    """All submissions awaiting reconciliation or review."""
    pubs = await store.list_publications()
    return [p for p in pubs if (p.get("submission_state") in UNRESOLVED_STATES)][:limit]
