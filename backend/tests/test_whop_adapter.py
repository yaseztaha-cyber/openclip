"""Whop adapter correctness tests: documented base URL, version pinning,
read-only auth check taxonomy, account guard, no invented endpoints.
No network, no credentials.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from automation.providers import OfficialWhopProvider, normalize_campaign
from automation.provider_client import ProviderTransportError


def _provider(**kw):
    kw.setdefault("api_key", "test-key-not-real")
    return OfficialWhopProvider(**kw)


def test_documented_defaults():
    p = OfficialWhopProvider(api_key="x")
    assert p.base_url == "https://api.whop.com/api/v1"
    assert p.api_version_date == "2026-07-01"
    headers = p._headers()
    assert headers["Api-Version-Date"] == "2026-07-01"
    assert headers["Authorization"] == "Bearer x"


def test_no_key_disables_everything():
    p = OfficialWhopProvider(api_key=None)
    assert p.discover() == []
    with pytest.raises(ProviderTransportError) as e:
        p.check_auth()
    assert e.value.kind == "never_sent"


def test_check_auth_success():
    seen = {}

    def fake(path):
        seen["path"] = path
        return 200, {"id": "biz_ABC", "title": "Acme",
                     "route": "acme", "status": "approved"}

    out = _provider(request_fn=fake).check_auth()
    assert seen["path"] == "/accounts/me"  # documented endpoint only
    assert out["outcome"] == "AUTHENTICATED"
    assert out["account_id"] == "biz_ABC"


def test_check_auth_account_guard(monkeypatch):
    monkeypatch.setenv("WHOP_ACCOUNT_ID", "biz_EXPECTED")

    def fake(path):
        return 200, {"id": "biz_OTHER", "status": "approved"}

    with pytest.raises(ProviderTransportError) as e:
        _provider(request_fn=fake).check_auth()
    assert e.value.status_code == 403

    def fake_ok(path):
        return 200, {"id": "biz_EXPECTED", "status": "approved"}

    assert _provider(request_fn=fake_ok).check_auth()["account_id"] == "biz_EXPECTED"


def test_check_auth_taxonomy():
    cases = [
        (401, {}, ("http", 401)),
        (403, {}, ("http", 403)),
        (429, {}, ("http", 429)),
        (500, {}, ("http", 500)),
        (503, {}, ("http", 503)),
        (200, {"no": "id"}, ("http", 200)),
    ]
    for status, body, (kind, code) in cases:
        with pytest.raises(ProviderTransportError) as e:
            _provider(request_fn=lambda p, s=status, b=body: (s, b)).check_auth()
        assert (e.value.kind, e.value.status_code) == (kind, code)


def test_check_auth_transport_doubt():
    def boom_timeout(path):
        raise TimeoutError("read timed out")

    with pytest.raises(ProviderTransportError) as e:
        _provider(request_fn=boom_timeout).check_auth()
    assert e.value.kind == "timeout"

    def boom_conn(path):
        raise ConnectionError("reset")

    with pytest.raises(ProviderTransportError) as e:
        _provider(request_fn=boom_conn).check_auth()
    assert e.value.kind == "connection"


def test_discover_never_calls_invented_endpoints():
    calls = []

    def fake(path):
        calls.append(path)
        return 200, {"campaigns": [{"campaign_id": "x"}]}

    assert _provider(request_fn=fake).discover() == []
    assert calls == []  # no /campaigns or any other guess


def test_normalize_preserves_whop_shape():
    c = normalize_campaign({"campaign_id": "w1", "name": "Plan",
                            "platforms": "tiktok"})
    assert c["campaign_id"] == "w1" and c["platforms"] == ["tiktok"]
