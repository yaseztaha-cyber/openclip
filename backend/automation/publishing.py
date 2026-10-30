"""
publishing.py — Publisher abstraction with centralized hard gates.

Phases 2+3: EVERY external publish path funnels through
assert_publish_allowed(...). No publisher duplicates guard logic.

  Publisher
  ├── MockPublisher                 (dry-run / tests only; still gated on recording)
  ├── ReplayPublisher               (replay only; still gated on recording)
  └── AuthorizedPlatformPublisher   (official mechanisms only)

Live publishing requires ALL of: AUTOMATION_MODE=live,
LIVE_PUBLISHING_ENABLED=true, configured adapter + credential, explicit
unexpired approval bound to the exact render+metadata+policy, existing
verified render artifact, a still-permitting policy, and a unique
idempotency key. Any failure BLOCKS before any external call.
"""

from __future__ import annotations

import datetime
import logging
import os
from abc import ABC, abstractmethod

from .scheduler import idempotency_key
from .side_effects import DryRunBlocked, is_live, require_live

logger = logging.getLogger(__name__)

APPROVAL_TTL_HOURS = float(os.getenv("APPROVAL_TTL_HOURS", "168"))


class PublishBlocked(Exception):
    def __init__(self, code: str, message: str, details=None):
        self.code = code
        super().__init__(message)
        self.payload = {"code": code, "message": message,
                        "details": details or {}}


def live_publishing_enabled() -> bool:
    return os.getenv("LIVE_PUBLISHING_ENABLED", "false").lower() == "true"


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True  # approvals without expiry are not publishable
    try:
        exp = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return now > exp
    except Exception:
        return True


def assert_publish_allowed(*, approval: dict | None, render_hash: str,
                           title: str, description: str,
                           campaign_status: str | None,
                           current_policy_version: int | None,
                           render_verified: bool,
                           idempotency_unique: bool,
                           adapter_enabled: bool,
                           credential_present: bool,
                           platform: str | None = None,
                           from_hashes=None) -> dict:
    """
    Centralized live-publish gate. Raises PublishBlocked on ANY failure.
    from_hashes: optional fn(text)->hash for title/description comparison.
    Returns {"ok": True} when every condition holds.
    """
    if not is_live():
        raise PublishBlocked("dry_run", "publishing blocked: AUTOMATION_MODE is not live")
    if not live_publishing_enabled():
        raise PublishBlocked("live_disabled", "LIVE_PUBLISHING_ENABLED is not true")
    if not adapter_enabled:
        raise PublishBlocked("adapter_disabled", "publisher adapter is not enabled")
    if not credential_present:
        raise PublishBlocked("missing_credential", "no authorized credential configured")
    if approval is None:
        raise PublishBlocked("no_approval", "explicit approval required")
    if (approval.get("status") or "").upper() != "APPROVED":
        raise PublishBlocked(
            "not_approved", f"approval status is {approval.get('status')!r}, need APPROVED")
    if _expired(approval.get("expires_at")):
        raise PublishBlocked("approval_expired", "approval has expired; re-approve")
    if approval.get("invalidated_reason"):
        raise PublishBlocked("approval_invalidated",
                             f"approval invalidated: {approval['invalidated_reason']}")
    if (approval.get("render_hash") or "") != (render_hash or ""):
        raise PublishBlocked("render_mismatch",
                             "approval was granted for a different render; re-approve")
    if from_hashes:
        if approval.get("title_hash") != from_hashes(title or ""):
            raise PublishBlocked("metadata_mismatch", "title changed after approval; re-approve")
        if approval.get("description_hash") != from_hashes(description or ""):
            raise PublishBlocked("metadata_mismatch",
                                 "description changed after approval; re-approve")
    if platform is not None and approval.get("platform"):
        if str(approval["platform"]).lower() != str(platform).lower():
            raise PublishBlocked("platform_mismatch",
                                 "approval was granted for a different platform; re-approve",
                                 {"approved_platform": approval.get("platform"),
                                  "requested_platform": platform})
    if (campaign_status or "").upper() not in ("ELIGIBLE", "APPROVED_FOR_PROCESSING",
                                               "PROCESSING", "COMPLETED", ""):
        raise PublishBlocked("campaign_not_permitted",
                             f"campaign status {campaign_status!r} does not permit publishing")
    if (current_policy_version is not None and approval.get("policy_version") is not None
            and int(approval["policy_version"]) != int(current_policy_version)):
        raise PublishBlocked("policy_changed",
                             "policy changed after approval; re-evaluate and re-approve")
    if not render_verified:
        raise PublishBlocked("render_unverified",
                             "current render missing or failed integrity verification")
    if not idempotency_unique:
        raise PublishBlocked("duplicate", "idempotency key already published",
                             {"submission_state": "ALREADY_PUBLISHED"})
    return {"ok": True}


class PublishResult(dict):
    pass


class Publisher(ABC):
    name = "base"
    external = True  # performs external side effects

    # Explicit capability contract. Subclasses override honestly; the API
    # surfaces SUPPORTED vs CONFIGURED vs AUTHENTICATED vs AVAILABLE vs
    # OPERATIONAL vs UNAVAILABLE instead of guessing.
    CAPABILITIES = {
        "supports_submission_lookup": False,
        "supports_idempotency_keys": False,
        "supports_submission_status": False,
        "supports_delete": False,
        "supports_analytics": False,
        "supports_retry_after": False,
    }

    @classmethod
    def capabilities(cls) -> dict:
        return dict(cls.CAPABILITIES)

    @abstractmethod
    def publish(self, clip_path: str, title: str, description: str,
                platform: str, idem_key: str, **kwargs) -> dict:
        ...

    def lookup_submission(self, *, idempotency_key=None,
                          provider_submission_id=None) -> dict | None:
        """Provider-evidence lookup for reconciliation. None = no record."""
        from .submissions import LookupUnavailable
        raise LookupUnavailable(f"{self.name} exposes no submission lookup")


class MockPublisher(Publisher):
    """Deterministic publisher for dry-run/tests. Gated: never touches network."""

    name = "mock"
    external = False
    CAPABILITIES = {
        "supports_submission_lookup": True,  # in-memory registry lookup
        "supports_idempotency_keys": True,
        "supports_submission_status": True,
        "supports_delete": False,
        "supports_analytics": False,
        "supports_retry_after": False,
    }

    def __init__(self):
        self.published: list[dict] = []
        self.calls = 0
        self._registry: dict[str, dict] = {}

    def publish(self, clip_path, title, description, platform, idem_key, **kwargs):
        self.calls += 1
        if idem_key in self._registry:
            return {**self._registry[idem_key], "duplicate": True}
        url = f"mock://{platform}/{idem_key[:12]}"
        rec = {"post_url": url, "platform": platform, "title": title,
               "status": "published", "mock": True,
               "provider_submission_id": f"mocksub_{idem_key[:12]}"}
        self.published.append(rec)
        self._registry[idem_key] = rec
        return rec

    def lookup_submission(self, *, idempotency_key=None,
                          provider_submission_id=None) -> dict | None:
        if idempotency_key and idempotency_key in self._registry:
            rec = self._registry[idempotency_key]
            return {"status": "CONFIRMED", "post_url": rec["post_url"],
                    "provider_submission_id": rec["provider_submission_id"]}
        if provider_submission_id:
            for rec in self._registry.values():
                if rec.get("provider_submission_id") == provider_submission_id:
                    return {"status": "CONFIRMED", "post_url": rec["post_url"],
                            "provider_submission_id": provider_submission_id}
        return None


class ReplayPublisher(Publisher):
    name = "replay"
    external = False
    CAPABILITIES = {
        "supports_submission_lookup": False,
        "supports_idempotency_keys": True,
        "supports_submission_status": False,
        "supports_delete": False,
        "supports_analytics": False,
        "supports_retry_after": False,
    }

    def __init__(self, urls: list[str] | None = None):
        self._urls = list(urls or [])
        self._i = 0
        self.calls = 0

    def publish(self, clip_path, title, description, platform, idem_key, **kwargs):
        self.calls += 1
        url = self._urls[self._i % len(self._urls)] if self._urls else f"replay://{platform}/{idem_key[:12]}"
        self._i += 1
        return {"post_url": url, "platform": platform, "title": title,
                "status": "published", "replay": True}


class AuthorizedPlatformPublisher(Publisher):
    """Live publisher: official mechanisms only, central gate enforced."""

    name = "authorized"
    external = True
    CAPABILITIES = {
        # Conservative defaults: a concrete platform adapter flips these to
        # True only when the official API actually offers the operation.
        "supports_submission_lookup": False,
        "supports_idempotency_keys": False,
        "supports_submission_status": False,
        "supports_delete": False,
        "supports_analytics": False,
        "supports_retry_after": True,
    }

    def publish(self, clip_path, title, description, platform, idem_key,
                approval_id: str | None = None, account_id: str | None = None,
                gate: dict | None = None) -> dict:
        # Central gate runs BEFORE any external call. The caller assembles
        # the evidence; this method re-asserts mode + live flags itself so
        # no path can bypass them. Provider I/O goes only through the
        # narrow ProviderClient boundary — never inline HTTP here.
        from .provider_client import (AuthorizedProviderClient,
                                      ProviderTransportError)
        require_live("publish")
        if not live_publishing_enabled():
            raise PublishBlocked("live_disabled", "LIVE_PUBLISHING_ENABLED is not true")
        if not approval_id:
            raise PublishBlocked("no_approval", "explicit approval_id required")
        token = os.getenv(f"PUBLISH_TOKEN_{platform.upper()}", "").strip()
        if not token:
            raise PublishBlocked("missing_credential",
                                 f"no publish token configured for {platform}")
        client = AuthorizedProviderClient(platform)
        try:
            res = client.submit({"clip_path": clip_path, "title": title,
                                 "description": description, "platform": platform,
                                 "idempotency_key": idem_key,
                                 "approval_id": approval_id,
                                 "account_id": account_id})
        except ProviderTransportError as exc:
            if exc.kind == "timeout":
                raise TimeoutError(f"provider timeout: {exc.body}")
            if exc.kind == "connection":
                raise ConnectionError(f"provider connection failed: {exc.body}")
            if exc.kind == "http":
                return {"status": "rejected",
                        "provider_status_code": exc.status_code,
                        "platform": platform,
                        "reason": exc.body[:200]}
            # never_sent / malformed: request did not reach provider state.
            logger.info("authorized publish platform=%s approval=%s (operator hook)",
                        platform, approval_id)
            return {"status": "needs_operator_hook", "platform": platform,
                    "reason": "wire platform official upload API at this hook"}
        logger.info("authorized publish platform=%s approval=%s responded",
                    platform, approval_id)
        return {"status": res.get("outcome", "pending"), "platform": platform,
                "provider_status_code": res.get("status_code"),
                "provider_submission_id": res.get("provider_submission_id"),
                "post_url": res.get("post_url"),
                "retry_after": res.get("retry_after"),
                "reason": "provider responded"}


def build_idem_key(campaign_id: str, clip_id: str, render_hash: str, platform: str) -> str:
    c = (campaign_id or "").strip().lower()
    k = (clip_id or "").strip()
    r = (render_hash or "").strip()
    p = (platform or "").strip().lower()
    return idempotency_key(c, k, r, p)


def canonical_idem_key(campaign_id: str, clip_id: str, render_hash: str,
                       platform: str) -> dict:
    """Normalized canonical representation behind the hashed key."""
    return {"campaign_id": (campaign_id or "").strip().lower(),
            "clip_id": (clip_id or "").strip(),
            "render_hash": (render_hash or "").strip(),
            "platform": (platform or "").strip().lower(),
            "key": build_idem_key(campaign_id, clip_id, render_hash, platform)}


def adapter_status(publisher: Publisher, platform: str) -> dict:
    """
    Explicit adapter state — never conflates unsupported with unavailable.
    Chain: SUPPORTED → CONFIGURED → AUTHENTICATED → AVAILABLE → OPERATIONAL.
    Simulators (mock/replay) are OPERATIONAL as simulators; unknown adapter
    names are UNAVAILABLE. Live adapters without credentials/mode surface
    their exact shortfall (never fake success).
    """
    from .side_effects import is_live
    caps = publisher.capabilities()
    if not publisher.external:
        return {"adapter": publisher.name, "platform": platform,
                "capabilities": caps, "state": "OPERATIONAL",
                "reason": "simulator adapter (no external I/O)"}
    token = os.getenv(f"PUBLISH_TOKEN_{platform.upper()}", "").strip()
    mode_ok = is_live() and live_publishing_enabled()
    allow = [p.strip().lower() for p in
             os.getenv("PUBLISH_PLATFORMS", "").split(",") if p.strip()]
    if not token:
        # Credential absent: CONFIGURED only if the operator registered the
        # platform at all (explicit allow-list env), else SUPPORTED.
        state = "CONFIGURED" if platform.lower() in allow else "SUPPORTED"
        return {"adapter": publisher.name, "platform": platform,
                "capabilities": caps, "state": state,
                "reason": f"no publish credential configured for {platform}"}
    if not mode_ok:
        return {"adapter": publisher.name, "platform": platform,
                "capabilities": caps, "state": "AUTHENTICATED",
                "reason": "credential present; live mode not enabled"}
    if allow and platform.lower() not in allow:
        return {"adapter": publisher.name, "platform": platform,
                "capabilities": caps, "state": "AVAILABLE",
                "reason": "credential and mode OK; platform not in PUBLISH_PLATFORMS"}
    return {"adapter": publisher.name, "platform": platform,
            "capabilities": caps, "state": "OPERATIONAL", "reason": "ready"}


def resolve_publisher(requested: str, *, live: bool) -> Publisher:
    """
    Resolve an adapter with NO silent fallback: LIVE never degrades to
    MOCK/REPLAY (a provider failure stays a provider failure), and mock
    requested in live mode stays mock (explicit simulation).
    """
    requested = (requested or "mock").lower()
    if requested == "authorized":
        if not live:
            raise PublishBlocked("live_disabled",
                                 "authorized adapter requires live mode")
        return AuthorizedPlatformPublisher()
    if requested == "replay":
        return ReplayPublisher()
    if requested == "mock":
        return MockPublisher()
    raise PublishBlocked("adapter_unavailable",
                         f"unknown publisher adapter '{requested}'",
                         {"state": "UNAVAILABLE"})
