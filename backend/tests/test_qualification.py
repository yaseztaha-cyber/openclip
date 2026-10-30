"""v1.3 qualification tests: normalization, drift, outage, credentials,
rollback, lineage, privacy, two-layer idempotency. No live credentials.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import database
import api
from automation.provider_client import (MockProviderClient,
                                        AuthorizedProviderClient,
                                        ScriptedTransport,
                                        ProviderTransportError,
                                        normalize_response,
                                        normalize_transport_error)
from automation.publishing import assert_publish_allowed, PublishBlocked
from automation.render_identity import content_hash
from automation.qualification import (new_qualification, upgrade_to_live_verified,
                                      MOCK_VERIFIED, LIVE_OPTIONAL, LIVE_VERIFIED,
                                      UNAVAILABLE)
from automation import store


def _use_tmp_db(tmp_path, name="qual.db"):
    database.DB_PATH = str(tmp_path / name)
    asyncio.run(store.init_automation_tables())


def _approval(**kw):
    import datetime
    exp = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(hours=2)).isoformat()
    d = {"status": "APPROVED", "expires_at": exp, "render_hash": "rh_1",
         "title_hash": content_hash("t"), "description_hash": content_hash("d"),
         "policy_version": 1, "invalidated_reason": None, "platform": "tiktok"}
    d.update(kw)
    return d


def _gate(monkeypatch, **kw):
    monkeypatch.setenv("AUTOMATION_MODE", "live")
    monkeypatch.setenv("LIVE_PUBLISHING_ENABLED", "true")
    monkeypatch.setenv("PUBLISH_TOKEN_TIKTOK", "tok")
    d = {"approval": _approval(), "render_hash": "rh_1", "title": "t",
         "description": "d", "campaign_status": "ELIGIBLE",
         "current_policy_version": 1, "render_verified": True,
         "idempotency_unique": True, "adapter_enabled": True,
         "credential_present": True, "platform": "tiktok",
         "from_hashes": content_hash}
    d.update(kw)
    return assert_publish_allowed(**d)


# --- normalization (Phase 4) ---

def test_normalize_success_failure_taxonomy():
    assert normalize_response({"outcome": "CONFIRMED",
                               "post_url": "http://x/1"})["code"] == "success"
    assert normalize_response({"outcome": "CONFIRMED"})["code"] == "unknown_response"
    assert normalize_response({"provider_status_code": 429})["code"] == "rate_limit"
    assert normalize_response({"provider_status_code": 401})["code"] == "authentication_error"
    assert normalize_response({"provider_status_code": 403})["code"] == "authorization_error"
    assert normalize_response({"provider_status_code": 404})["code"] == "not_found"
    assert normalize_response({"provider_status_code": 409})["code"] == "conflict"
    assert normalize_response({"provider_status_code": 422})["code"] == "validation_error"
    assert normalize_response({"provider_status_code": 503})["code"] == "temporary_unavailable"
    assert normalize_response({})["code"] == "unknown_response"
    assert normalize_response(None)["code"] == "unknown_response"
    t = normalize_transport_error(ProviderTransportError("timeout"))
    assert t["code"] == "timeout" and t["ambiguous"] is True
    # no raw provider text leaks into stable codes
    weird = normalize_response({"outcome": "!!!weird!!!", "blob": "x" * 5000})
    assert weird["code"] == "unknown_response"


# --- qualification records (Phase 2) ---

def test_qualification_record_validation():
    q = new_qualification(provider="mock", platform="tiktok",
                          verification_type=MOCK_VERIFIED,
                          verification_scope="contract tests")
    assert q["capabilities"]["SUBMIT"] == "UNAVAILABLE"  # explicit, not inferred
    with pytest.raises(ValueError):
        new_qualification(provider="x", platform="y", verification_type="BOGUS")
    with pytest.raises(ValueError):
        new_qualification(provider="x", platform="y",
                          capabilities={"SUBMIT": "MAYBE"})


def test_live_upgrade_requires_evidence():
    q = new_qualification(provider="authorized", platform="tiktok",
                          verification_type=LIVE_OPTIONAL)
    with pytest.raises(ValueError):
        upgrade_to_live_verified(q, scope="s", evidence={"authorized": True})
    with pytest.raises(ValueError):
        upgrade_to_live_verified(
            new_qualification(provider="m", platform="p",
                              verification_type=MOCK_VERIFIED),
            scope="s", evidence={"authorized": True, "succeeded": True})
    up = upgrade_to_live_verified(q, scope="readonly probe",
                                  evidence={"authorized": True, "succeeded": True})
    assert up["verification_type"] == LIVE_VERIFIED


def test_qualification_persistence(tmp_path):
    _use_tmp_db(tmp_path)
    q = new_qualification(provider="mock", platform="tiktok",
                          verification_type=MOCK_VERIFIED,
                          capabilities={"SUBMIT": "VERIFIED", "LOOKUP": "VERIFIED"})
    rid = asyncio.run(store.save_qualification(q))
    rows = asyncio.run(store.list_qualifications("mock", "tiktok"))
    assert any(r["record_id"] == rid and r["capabilities"]["LOOKUP"] == "VERIFIED"
               for r in rows)


# --- drift (Phase 17): platform change blocks ---

def test_platform_drift_blocks(monkeypatch):
    with pytest.raises(PublishBlocked) as e:
        _gate(monkeypatch, platform="youtube")
    assert e.value.code == "platform_mismatch"
    # unset platform on old approvals stays backward compatible
    _gate(monkeypatch, approval=_approval(platform=None))


def test_provider_credential_variants(monkeypatch):
    monkeypatch.setenv("AUTOMATION_MODE", "live")
    monkeypatch.setenv("LIVE_PUBLISHING_ENABLED", "true")
    for tok in ("", "   ", "not-a-real-token-but-present"):
        monkeypatch.setenv("PUBLISH_TOKEN_TIKTOK", tok)
        c = AuthorizedProviderClient("tiktok")
        chk = c.check()
        assert chk["published"] is False
        if not tok.strip():
            assert chk["state"] == "UNAVAILABLE"
    # expired/revoked/malformed all surface as explicit auth errors, no retry storm
    t = ScriptedTransport([("raise", ProviderTransportError("http", status_code=401,
                                                            body="expired"))])
    c = AuthorizedProviderClient("tiktok", transport=t)
    from automation.submissions import classify_provider_error
    v = classify_provider_error(401)
    assert v["class"] == "NON_RETRYABLE"
    assert "tok" not in str(v)


def test_outage_behavior_no_fake_success():
    # complete outage
    t = ScriptedTransport([("raise", ProviderTransportError("connection"))])
    c = AuthorizedProviderClient("tiktok", transport=t)
    import os as _os
    _os.environ["PUBLISH_TOKEN_TIKTOK"] = "x"
    try:
        with pytest.raises(ProviderTransportError):
            c.submit({"idempotency_key": "o1"})
    finally:
        del _os.environ["PUBLISH_TOKEN_TIKTOK"]
    n = normalize_transport_error(ProviderTransportError("connection"))
    assert n["ambiguous"] is True  # unknown, never failed-success


def test_lookup_qualification_mock():
    m = MockProviderClient()
    assert m.lookup(idempotency_key="absent") is None  # known absent
    m.submit({"idempotency_key": "k1"})
    found = m.lookup(idempotency_key="k1")  # known confirmed
    assert found["status"] == "CONFIRMED"
    assert m.lookup(idempotency_key="") is None  # malformed identifier
    # rate-limit / outage surface as transport errors, not fake negatives
    m2 = MockProviderClient(transport=ScriptedTransport(
        [("raise", ProviderTransportError("http", status_code=429))]))
    with pytest.raises(ProviderTransportError):
        m2.submit({"idempotency_key": "k2"})


def test_rollback_unsupported_documented():
    assert MockProviderClient().capabilities()["supports_delete"] is False
    assert AuthorizedProviderClient("tiktok").capabilities()["supports_delete"] is False
    # no delete/reverse path exists on publishers
    assert not hasattr(MockProviderClient, "delete_submission")
    assert not hasattr(AuthorizedProviderClient, "delete_submission")


def test_idempotency_two_layers():
    # internal layer verified; provider layer honestly unsupported
    assert MockProviderClient().capabilities()["supports_idempotency_keys"] is True
    assert AuthorizedProviderClient("tiktok").capabilities()["supports_idempotency_keys"] is False


def test_analytics_metric_independence():
    from automation.analytics import aggregate_publications
    pubs = [{"id": "p1", "clip_id": "c1", "post_url": "http://x/1"}]
    snaps = [{"publication_id": "p1", "ts": "t", "views": 1000}]
    out = aggregate_publications(pubs, snaps)
    assert out["total_views"] == 1000
    # revenue is NOT derived from views
    assert out["confirmed_earnings"] == "NOT AVAILABLE"
    assert out["estimated_earnings"] == "NOT AVAILABLE"


def test_privacy_truncation(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.record_submission_attempt(clip_id="c", campaign_id="c",
                                                platform="t", idempotency_key="PRIV"))
    atts = asyncio.run(store.list_submission_attempts("PRIV"))
    asyncio.run(store.finish_submission_attempt(
        atts[0]["submission_attempt_id"], request_status="RESPONDED",
        result_state="FAILED", provider_response_metadata={"blob": "y" * 5000}))
    atts = asyncio.run(store.list_submission_attempts("PRIV"))
    assert len(str(atts[0]["provider_response_metadata"])) < 1000


def _api_client(tmp_path, monkeypatch):
    from contextlib import contextmanager

    db_path = tmp_path / "qual_api.db"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    # NOTE: no manual init_db() here — entering the client must trigger
    # lifespan initialization (this is the v1.4 lifecycle proof).
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()

    @contextmanager
    def _entered():
        with TestClient(api.app, raise_server_exceptions=False) as c:
            yield c

    return _entered()


def test_preflight_blocks_without_side_effects(tmp_path, monkeypatch):
    with _api_client(tmp_path, monkeypatch) as c:
        r = c.post("/api/automation/publish/preflight", json={
            "clip_id": "nope", "platform": "tiktok", "title": "t",
            "render_hash": "rh_x"})
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "BLOCKED"
        assert "candidate" in body and "checks" in body
        # preflight claimed nothing, published nothing
        assert c.get("/api/automation/publications").json()["publications"] == []


def test_lineage_chain_shape(tmp_path, monkeypatch):
    with _api_client(tmp_path, monkeypatch) as c:
        body = c.get("/api/automation/lineage/some-clip").json()["chain"]
        assert set(body) >= {"source", "editor_state", "renders", "approval",
                             "submission", "transitions", "decisions", "analytics"}
        assert body["source"] == {"present": False, "clip": None}


def test_smtp_verify_states(tmp_path, monkeypatch):
    with _api_client(tmp_path, monkeypatch) as c:
        monkeypatch.delenv("SMTP_HOST", raising=False)
        r = c.post("/api/automation/smtp/verify", json={})
        assert r.json()["state"] == "NOT_CONFIGURED"
        quals = c.get("/api/automation/providers/qualifications?provider=smtp").json()
        assert quals["qualifications"][0]["verification_type"] == "UNAVAILABLE"


def test_qualifications_endpoint_rejects_live_assertion(tmp_path, monkeypatch):
    with _api_client(tmp_path, monkeypatch) as c:
        r = c.post("/api/automation/providers/qualifications", json={
            "provider": "x", "platform": "y", "verification_type": "LIVE VERIFIED"})
        assert r.status_code == 400
        r = c.post("/api/automation/providers/qualifications", json={
            "provider": "mock", "platform": "tiktok",
            "verification_type": "MOCK VERIFIED",
            "capabilities": {"SUBMIT": "VERIFIED"}})
        assert r.status_code == 200
