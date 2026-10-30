"""
providers.py — Campaign discovery provider abstraction.

  CampaignProvider (ABC)
  ├── MockCampaignProvider     (deterministic fixtures for tests/dev)
  ├── ReplayCampaignProvider   (replays a stored JSON list)
  └── OfficialWhopProvider     (authorized API only; env-gated, no scraping)

Normalized campaign schema:
  campaign_id, name, description, requirements, allowed_content, platforms,
  reward_rules, deadline, source_url, content_sources, eligibility_metadata,
  affiliations, brands, entities
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

NORMALIZED_KEYS = [
    "campaign_id", "name", "description", "requirements", "allowed_content",
    "platforms", "reward_rules", "deadline", "source_url", "content_sources",
    "eligibility_metadata", "affiliations", "brands", "entities",
]


def normalize_campaign(raw: dict) -> dict:
    out = {k: raw.get(k) for k in NORMALIZED_KEYS}
    out["campaign_id"] = str(raw.get("campaign_id") or raw.get("id") or "unknown")
    for k in ("platforms", "content_sources", "affiliations", "brands", "entities"):
        v = out.get(k)
        if v is None:
            out[k] = []
        elif not isinstance(v, list):
            out[k] = [v]
    return out


class CampaignProvider(ABC):
    name = "base"

    @abstractmethod
    def discover(self) -> list[dict]:
        """Return a list of normalized campaign dicts."""


class MockCampaignProvider(CampaignProvider):
    """Deterministic in-memory campaigns for tests and dry runs."""

    name = "mock"

    def __init__(self, campaigns: list[dict] | None = None):
        self._campaigns = [normalize_campaign(c) for c in (campaigns or [])]

    def discover(self) -> list[dict]:
        return [dict(c) for c in self._campaigns]


class ReplayCampaignProvider(CampaignProvider):
    """Replay campaigns from a stored JSON file (audit / regression)."""

    name = "replay"

    def __init__(self, path: str):
        self.path = path

    def discover(self) -> list[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.warning("replay file unreadable %s: %s", self.path, exc)
            return []
        items = data if isinstance(data, list) else data.get("campaigns", [])
        return [normalize_campaign(c) for c in items]


class OfficialWhopProvider(CampaignProvider):
    """
    Authorized Whop integration (docs: https://docs.whop.com/).

    - Base URL defaults to the documented Current API:
      https://api.whop.com/api/v1 (NOT /v5).
    - Every request pins Api-Version-Date (WHOP_API_VERSION_DATE).
    - Auth validation is read-only: GET /api/v1/accounts/me.
    - There is NO invented /campaigns endpoint: Whop exposes products,
      plans, memberships — not AutoClip campaigns. Campaign discovery
      stays disabled until the operator explicitly maps a Whop resource
      (WHOP_CAMPAIGNS_PATH) and verifies normalization.
    - Disabled unless WHOP_API_KEY is configured. Never scrapes.
    """

    name = "whop_official"
    DEFAULT_BASE_URL = "https://api.whop.com/api/v1"
    DEFAULT_API_VERSION_DATE = "2026-07-01"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 api_version_date: str | None = None, request_fn=None):
        self.api_key = api_key or os.getenv("WHOP_API_KEY")
        self.base_url = (base_url or os.getenv("WHOP_API_BASE",
                                               self.DEFAULT_BASE_URL)).rstrip("/")
        self.api_version_date = (
            api_version_date or os.getenv("WHOP_API_VERSION_DATE",
                                          self.DEFAULT_API_VERSION_DATE))
        # Injectable transport for deterministic tests (no network).
        self._request_fn = request_fn or self._http_get

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Api-Version-Date": self.api_version_date}

    def _http_get(self, path: str) -> tuple[int, dict]:
        import httpx  # type: ignore
        resp = httpx.get(f"{self.base_url}{path}", headers=self._headers(),
                         timeout=30)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return resp.status_code, body if isinstance(body, dict) else {}

    def check_auth(self) -> dict:
        """Read-only auth validation. Returns normalized account evidence.
        Raises ProviderTransportError on transport/auth failures. Never
        publishes, creates, or modifies anything."""
        from automation.provider_client import ProviderTransportError
        if not (self.api_key or "").strip():
            raise ProviderTransportError("never_sent",
                                         body="WHOP_API_KEY not configured")
        try:
            status, body = self._request_fn("/accounts/me")
        except ProviderTransportError:
            raise
        except TimeoutError as exc:
            raise ProviderTransportError("timeout", body=str(exc)[:200])
        except (ConnectionError, OSError) as exc:
            raise ProviderTransportError("connection", body=str(exc)[:200])
        except Exception as exc:
            raise ProviderTransportError("malformed", body=str(exc)[:200])
        if status == 200 and isinstance(body.get("id"), str):
            account_id = body["id"]
            expected = (os.getenv("WHOP_ACCOUNT_ID", "") or "").strip()
            if expected and account_id != expected:
                raise ProviderTransportError(
                    "http", status_code=403,
                    body=f"authenticated as {account_id}, "
                         f"expected {expected}; refusing to proceed")
            return {"outcome": "AUTHENTICATED", "provider": "whop",
                    "account_id": account_id,
                    "title": body.get("title"), "route": body.get("route"),
                    "status": body.get("status")}
        if status in (401, 403):
            raise ProviderTransportError("http", status_code=status,
                                         body="whop rejected credentials")
        if status == 429:
            raise ProviderTransportError("http", status_code=429,
                                         body="whop rate limited")
        if status in (500, 502, 503, 504):
            raise ProviderTransportError("http", status_code=status,
                                         body="whop transient error")
        raise ProviderTransportError("http", status_code=status or 0,
                                     body="unexpected whop response")

    def discover(self) -> list[dict]:
        if not (self.api_key or "").strip():
            logger.info("OfficialWhopProvider disabled: WHOP_API_KEY not configured")
            return []
        # No campaign resource is mapped yet: return [] rather than call
        # an invented endpoint. Operator mapping goes through
        # WHOP_CAMPAIGNS_PATH only with verified normalization.
        logger.info("OfficialWhopProvider: no Whop campaign resource mapped; "
                    "discovery disabled until operator maps WHOP_CAMPAIGNS_PATH")
        return []


def get_provider(name: str = "mock", **kwargs) -> CampaignProvider:
    if name == "replay":
        return ReplayCampaignProvider(kwargs.get("path", ""))
    if name == "whop_official":
        return OfficialWhopProvider()
    if isinstance(kwargs.get("campaigns"), list):
        return MockCampaignProvider(kwargs["campaigns"])
    return MockCampaignProvider([])
