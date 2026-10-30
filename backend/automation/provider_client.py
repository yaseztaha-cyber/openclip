"""
provider_client.py — Narrow provider interface (v1.2 §5).

Automation business logic knows ONLY: submit / lookup / status /
capabilities / analytics / health. All provider-specific HTTP lives
inside the client; nothing outside this module constructs provider
requests. Transports are injectable so contract tests run deterministically
without network or credentials.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class ProviderTransportError(Exception):
    def __init__(self, kind: str, status_code=None, body: str = "",
                 retry_after=None):
        self.kind = kind  # timeout|connection|http|malformed|never_sent
        self.status_code = status_code
        self.body = (body or "")[:500]
        self.retry_after = retry_after
        super().__init__(f"transport {kind} status={status_code}")


# Stable internal codes. Business logic uses ONLY these — never raw
# provider-specific response text.
N_SUCCESS = "success"
N_FAILURE = "failure"
N_RATE_LIMIT = "rate_limit"
N_AUTH_ERROR = "authentication_error"
N_FORBIDDEN = "authorization_error"
N_NOT_FOUND = "not_found"
N_CONFLICT = "conflict"
N_VALIDATION_ERROR = "validation_error"
N_TEMPORARY = "temporary_unavailable"
N_TIMEOUT = "timeout"
N_UNKNOWN_RESPONSE = "unknown_response"


def normalize_response(result: dict | None) -> dict:
    """Normalize a provider response to a stable internal code + redacted detail."""
    from automation.submissions import redact_metadata
    if not isinstance(result, dict) or not result:
        return {"code": N_UNKNOWN_RESPONSE, "retryable": False,
                "detail": "empty or malformed provider response"}
    code = result.get("provider_status_code")
    outcome = str(result.get("outcome", result.get("status", ""))).lower()
    if outcome in ("confirmed", "published", "ok", "success", "live",
                   "complete", "completed"):
        if not result.get("post_url") and outcome in ("confirmed", "published"):
            return {"code": N_UNKNOWN_RESPONSE, "retryable": False,
                    "detail": "success claimed without object URL"}
        return {"code": N_SUCCESS, "retryable": False,
                "provider_submission_id": result.get("provider_submission_id"),
                "post_url": result.get("post_url")}
    if code == 429 or outcome == "rate_limited":
        return {"code": N_RATE_LIMIT, "retryable": True,
                "retry_after": result.get("retry_after"),
                "detail": redact_metadata(result)}
    if code in (401,) or outcome in ("unauthenticated", "auth_error"):
        return {"code": N_AUTH_ERROR, "retryable": False,
                "detail": "authentication failed (credential invalid/expired)"}
    if code in (403,) or outcome in ("forbidden",):
        return {"code": N_FORBIDDEN, "retryable": False,
                "detail": "authorization refused"}
    if code in (404,) or outcome in ("not_found",):
        return {"code": N_NOT_FOUND, "retryable": False,
                "detail": "provider has no such object"}
    if code in (409,) or outcome in ("conflict", "duplicate"):
        return {"code": N_CONFLICT, "retryable": False,
                "detail": "provider reports an existing submission"}
    if code in (400, 422) or outcome in ("validation_error", "invalid",
                                         "rejected"):
        return {"code": N_VALIDATION_ERROR, "retryable": False,
                "detail": redact_metadata(result)}
    if code in (500, 502, 503, 504) or outcome in ("temporary", "unavailable"):
        return {"code": N_TEMPORARY, "retryable": True,
                "retry_after": result.get("retry_after"),
                "detail": f"provider transient ({code or outcome})"}
    return {"code": N_UNKNOWN_RESPONSE, "retryable": False,
            "detail": redact_metadata(result)}


def normalize_transport_error(exc: ProviderTransportError) -> dict:
    if exc.kind == "timeout":
        return {"code": N_TIMEOUT, "retryable": False, "ambiguous": True,
                "detail": "timeout after possible transmission"}
    if exc.kind == "connection":
        return {"code": N_TIMEOUT, "retryable": False, "ambiguous": True,
                "detail": "connection failed; result unknown"}
    if exc.kind == "http":
        return normalize_response({"provider_status_code": exc.status_code,
                                   "outcome": "http"})
    return {"code": N_UNKNOWN_RESPONSE, "retryable": False,
            "detail": f"transport {exc.kind}"}


class ProviderClient(ABC):
    """Narrow interface every provider adapter implements."""

    name = "base"

    @abstractmethod
    def capabilities(self) -> dict:
        ...

    @abstractmethod
    def submit(self, payload: dict) -> dict:
        """Returns normalized {outcome, provider_submission_id?, post_url?,
        status_code?, retry_after?, raw_meta?} or raises
        ProviderTransportError."""

    @abstractmethod
    def lookup(self, *, idempotency_key=None,
               provider_submission_id=None) -> dict | None:
        ...

    def status(self, provider_submission_id: str) -> dict | None:
        return self.lookup(provider_submission_id=provider_submission_id)

    def analytics(self, provider_submission_id: str) -> dict:
        return {"status": "UNAVAILABLE", "reason": "no analytics operation"}

    def health(self) -> dict:
        caps = self.capabilities()
        return {"adapter": self.name, "state": "UNKNOWN",
                "capabilities": caps}


class ScriptedTransport:
    """Deterministic transport double for contract tests."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, operation: str, payload: dict) -> dict:
        self.calls.append({"operation": operation, "payload": dict(payload)})
        if not self.script:
            raise ProviderTransportError("never_sent", body="empty script")
        action = self.script.pop(0)
        if isinstance(action, dict):
            return dict(action)  # literal recorded response
        if action[0] == "raise":
            raise action[1]
        out = {"outcome": action[1]}
        if len(action) > 2:
            out.update(action[2] or {})
        return out


class MockProviderClient(ProviderClient):
    """In-memory provider semantics for dry-run/tests. No I/O."""

    name = "mock"

    def __init__(self, transport=None):
        # Default transport succeeds deterministically (no I/O); tests may
        # inject a ScriptedTransport for failure cases.
        self.transport = transport or (lambda op, p: {"outcome": "CONFIRMED"})
        self._registry: dict[str, dict] = {}

    def capabilities(self) -> dict:
        return {"supports_submission_lookup": True,
                "supports_idempotency_keys": True,
                "supports_submission_status": True,
                "supports_delete": False,
                "supports_analytics": False,
                "supports_retry_after": False}

    def submit(self, payload: dict) -> dict:
        key = str(payload.get("idempotency_key") or "")
        if key in self._registry:
            return {**self._registry[key], "duplicate": True}
        res = self.transport("submit", payload)
        rec = {"outcome": res.get("outcome", "CONFIRMED"),
               "provider_submission_id": f"mocksub_{abs(hash(key)) % 10**8:08d}",
               "post_url": f"mock://post/{abs(hash(key)) % 10**8:08d}",
               "status_code": 200}
        self._registry[key] = rec
        return rec

    def lookup(self, *, idempotency_key=None,
               provider_submission_id=None) -> dict | None:
        if idempotency_key and idempotency_key in self._registry:
            return {"status": "CONFIRMED", **self._registry[idempotency_key]}
        for rec in self._registry.values():
            if rec.get("provider_submission_id") == provider_submission_id:
                return {"status": "CONFIRMED", **rec}
        return None

    def health(self) -> dict:
        return {"adapter": self.name, "state": "OPERATIONAL",
                "capabilities": self.capabilities(),
                "reason": "simulator (no external I/O)"}


class ReplayProviderClient(ProviderClient):
    """Replays recorded provider responses. No I/O."""

    name = "replay"

    def __init__(self, recorded: list[dict] | None = None):
        self.recorded = list(recorded or [])
        self._i = 0

    def capabilities(self) -> dict:
        return {"supports_submission_lookup": False,
                "supports_idempotency_keys": True,
                "supports_submission_status": False,
                "supports_delete": False,
                "supports_analytics": False,
                "supports_retry_after": False}

    def submit(self, payload: dict) -> dict:
        if not self.recorded:
            return {"outcome": "CONFIRMED",
                    "provider_submission_id": "replay_0",
                    "post_url": "replay://post/0", "status_code": 200}
        out = dict(self.recorded[self._i % len(self.recorded)])
        self._i += 1
        return out

    def lookup(self, *, idempotency_key=None,
               provider_submission_id=None) -> dict | None:
        from automation.submissions import LookupUnavailable
        raise LookupUnavailable("replay adapter exposes no lookup")

    def health(self) -> dict:
        return {"adapter": self.name, "state": "OPERATIONAL",
                "capabilities": self.capabilities(),
                "reason": "simulator (no external I/O)"}


class AuthorizedProviderClient(ProviderClient):
    """
    Live boundary. Without credentials the adapter reports UNAVAILABLE and
    every operation refuses — it never fabricates. With credentials, only
    the operator-wired transport performs I/O; `check()` is always a safe
    read-only probe and never publishes.
    """

    name = "authorized"

    def __init__(self, platform: str, transport=None):
        import os
        self.platform = platform
        self.token_present = bool(os.getenv(f"PUBLISH_TOKEN_{platform.upper()}", "").strip())
        self.transport = transport

    def capabilities(self) -> dict:
        return {"supports_submission_lookup": False,
                "supports_idempotency_keys": False,
                "supports_submission_status": False,
                "supports_delete": False,
                "supports_analytics": False,
                "supports_retry_after": True}

    def _require(self, op: str):
        if not self.token_present:
            raise ProviderTransportError("never_sent",
                                         body=f"no credential for {self.platform}")
        if self.transport is None:
            raise ProviderTransportError(
                "never_sent",
                body="no operator transport wired; refusing to improvise HTTP")

    def submit(self, payload: dict) -> dict:
        self._require("submit")
        t0 = time.time()
        try:
            res = self.transport("submit", payload)
        except ProviderTransportError:
            raise
        except TimeoutError as exc:
            raise ProviderTransportError("timeout", body=str(exc)[:200])
        except (ConnectionError, OSError) as exc:
            raise ProviderTransportError("connection", body=str(exc)[:200])
        except Exception as exc:
            raise ProviderTransportError("malformed", body=str(exc)[:200])
        res["duration_ms"] = int((time.time() - t0) * 1000)
        return res

    def lookup(self, *, idempotency_key=None,
               provider_submission_id=None) -> dict | None:
        from automation.submissions import LookupUnavailable
        raise LookupUnavailable("authorized adapter declares no lookup")

    def check(self) -> dict:
        """Safe read-only verification: config → auth → capabilities.
        Never publishes."""
        if not self.token_present:
            return {"adapter": self.name, "platform": self.platform,
                    "state": "UNAVAILABLE",
                    "reason": "no credential configured",
                    "published": False}
        if self.transport is None:
            return {"adapter": self.name, "platform": self.platform,
                    "state": "AUTHENTICATED",
                    "reason": "credential present; no transport wired for probes",
                    "published": False}
        try:
            res = self.transport("check", {"platform": self.platform})
            return {"adapter": self.name, "platform": self.platform,
                    "state": "AVAILABLE", "probe": res, "published": False}
        except ProviderTransportError as exc:
            return {"adapter": self.name, "platform": self.platform,
                    "state": "UNAVAILABLE", "reason": f"{exc.kind}",
                    "published": False}

    def health(self) -> dict:
        if not self.token_present:
            return {"adapter": self.name, "state": "UNAVAILABLE",
                    "capabilities": self.capabilities(),
                    "reason": "no credential configured"}
        return {"adapter": self.name, "state": "AUTHENTICATED",
                "capabilities": self.capabilities(),
                "reason": "credential present; live mode gates use"}
