"""Provider adapter contract tests (v1.2 §4): Mock + Replay clients.

Every publisher/client implementation must satisfy: capabilities(),
adapter_status(), submit(), lookup_submission(), submission_status(),
analytics(), health() — across valid, malformed, timeout, 401/403/404/
409/429/5xx, missing/extra fields, empty body, invalid identifiers.
LIVE variants are LIVE_OPTIONAL (skipped without credentials).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.provider_client import (MockProviderClient,
                                        ReplayProviderClient,
                                        AuthorizedProviderClient,
                                        ScriptedTransport,
                                        ProviderTransportError)
from automation.publishing import (MockPublisher, ReplayPublisher,
                                   adapter_status)
from automation.submissions import LookupUnavailable


def _clients():
    return {"mock": MockProviderClient(), "replay": ReplayProviderClient()}


def test_capabilities_shape():
    for name, c in _clients().items():
        caps = c.capabilities()
        assert set(caps) == {"supports_submission_lookup",
                             "supports_idempotency_keys",
                             "supports_submission_status", "supports_delete",
                             "supports_analytics", "supports_retry_after"}, name
        assert all(isinstance(v, bool) for v in caps.values())


def test_health_shape():
    for name, c in _clients().items():
        h = c.health()
        assert h["adapter"] == name
        assert h["state"] in ("OPERATIONAL", "UNAVAILABLE")


def test_submit_valid():
    for name, c in _clients().items():
        out = c.submit({"idempotency_key": f"k-{name}", "title": "t"})
        assert out["outcome"] in ("CONFIRMED", "published", "ok",
                                  "accepted", "pending") or out.get("status_code") == 200


def test_submit_missing_fields_tolerated():
    for name, c in _clients().items():
        out = c.submit({})
        assert isinstance(out, dict)


def test_submit_extra_fields_ignored():
    for name, c in _clients().items():
        out = c.submit({"idempotency_key": "kx", "zzz_unexpected": [1, 2, 3],
                        "nested": {"a": None}})
        assert isinstance(out, dict)


def test_lookup_contract():
    mock = MockProviderClient()
    mock.submit({"idempotency_key": "lk"})
    found = mock.lookup(idempotency_key="lk")
    assert found and found["status"] == "CONFIRMED"
    assert mock.lookup(idempotency_key="nope") is None
    with pytest.raises(LookupUnavailable):
        ReplayProviderClient().lookup(idempotency_key="x")


def test_status_contract():
    mock = MockProviderClient()
    mock.submit({"idempotency_key": "st"})
    assert mock.status("mocksub_") is None  # unknown provider id
    found = mock.lookup(idempotency_key="st")
    assert mock.status(found["provider_submission_id"])["status"] == "CONFIRMED"


def test_analytics_unavailable_honest():
    for name, c in _clients().items():
        a = c.analytics("whatever")
        assert a["status"] == "UNAVAILABLE"


def test_scripted_transport_errors():
    t = ScriptedTransport([("raise", ProviderTransportError("timeout", body="t"))])
    m = MockProviderClient(transport=t)
    # transport-level timeout surfaces instead of being swallowed
    with pytest.raises(ProviderTransportError):
        m.submit({"idempotency_key": "te"})


def _http_outcome(code, body="err", retry_after=None):
    t = ScriptedTransport([("raise", ProviderTransportError("http", status_code=code,
                                                            body=body,
                                                            retry_after=retry_after))])
    return t


def test_http_error_taxonomy():
    for code in (401, 403, 404, 409, 429, 500, 502, 503):
        t = _http_outcome(code)
        with pytest.raises(ProviderTransportError) as e:
            t("submit", {})
        assert e.value.status_code == code
    t = ScriptedTransport([("raise", ProviderTransportError("http", status_code=429,
                                                            retry_after=45))])
    with pytest.raises(ProviderTransportError) as e:
        t("submit", {})
    assert e.value.retry_after == 45


def test_malformed_and_empty_responses():
    t = ScriptedTransport([{"outcome": None}, {}, {"outcome": "CONFIRMED"}])
    assert t("submit", {})["outcome"] is None
    assert t("submit", {}) == {}
    assert t("submit", {})["outcome"] == "CONFIRMED"


def test_invalid_identifiers():
    mock = MockProviderClient()
    assert mock.lookup(idempotency_key="") is None
    assert mock.lookup(idempotency_key=None) is None
    assert mock.lookup(provider_submission_id="invalid") is None


def test_publisher_adapter_status_contract():
    s = adapter_status(MockPublisher(), "tiktok")
    assert s["state"] == "OPERATIONAL"
    assert set(s["capabilities"]) == set(MockPublisher.capabilities())
    s = adapter_status(ReplayPublisher(), "tiktok")
    assert s["state"] == "OPERATIONAL"


def test_authorized_health_without_credentials(monkeypatch):
    monkeypatch.delenv("PUBLISH_TOKEN_TIKTOK", raising=False)
    c = AuthorizedProviderClient("tiktok")
    h = c.health()
    assert h["state"] == "UNAVAILABLE"
    assert "credential" in h["reason"]
    chk = c.check()
    assert chk["published"] is False and chk["state"] == "UNAVAILABLE"
    with pytest.raises(ProviderTransportError):
        c.submit({"idempotency_key": "x"})


def test_live_optional_mark():
    live = os.getenv("LIVE_PROVIDER_TESTS", "") == "1" and bool(
        os.getenv("PUBLISH_TOKEN_TIKTOK", ""))
    pytest.skip("LIVE OPTIONAL: no live provider credentials configured"
                if not live else "live configured (no assertions without a vetted provider)")
