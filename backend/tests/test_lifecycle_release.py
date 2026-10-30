"""v1.4 lifecycle/release tests: lifespan idempotency, graceful shutdown,
operator gating, envelope consistency, versions, request IDs, schema guard.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import database
import api
from automation import store


def _boot(tmp_path, monkeypatch, **env):
    db_path = tmp_path / "lc.db"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()
    return TestClient(api.app, raise_server_exceptions=False)


def _routes():
    return sorted({getattr(r, "path", "") for r in api.app.routes})


def test_lifespan_start_stop_idempotent(tmp_path, monkeypatch):
    c = _boot(tmp_path, monkeypatch)
    before = _routes()
    for _ in range(3):
        with c:
            assert c.get("/api/automation/liveness").status_code == 200
    assert _routes() == before  # no duplicate routers/handlers
    assert api._render_slots is not None and api._pipeline_slots is not None
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "lc.db"))
    try:
        tables = {x[0] for x in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert {"settings", "projects", "automation_campaigns",
            "automation_meta"} <= tables


def test_shutdown_preserves_unknown_and_recoverable(tmp_path, monkeypatch):
    c = _boot(tmp_path, monkeypatch)
    with c:
        asyncio.run(store.claim_publication_slot("k", "c", "tiktok", "SHUT_K"))
        asyncio.run(store.update_publication_by_key("SHUT_K", status="unknown",
                                                    submission_state="UNKNOWN_EXTERNAL_RESULT"))
        asyncio.run(store.upsert_job("shut1", "publish_clip", "running",
                                     "c", "k"))
        asyncio.run(store.heartbeat_job("shut1", "worker-x", "publish"))
    # "restart": enter again; UNKNOWN intact, scheduling refuses blind rerun
    with c:
        pub = asyncio.run(store.find_publication_by_key("SHUT_K"))
        assert pub["submission_state"] == "UNKNOWN_EXTERNAL_RESULT"
        from automation.scheduler import AutomationScheduler
        rec = AutomationScheduler.recover_interrupted(
            [{"job_id": "shut1", "status": "running", "kind": "publish_clip",
              "attempt": 5, "heartbeat_at": "2020-01-01T00:00:00+00:00"}])
        assert rec[0]["action"] == "require_review"


def test_operator_token_gating(tmp_path, monkeypatch):
    c = _boot(tmp_path, monkeypatch, OPERATOR_TOKEN="op-secret-123")
    with c:
        r = c.put("/api/automation/policy", json={"policy": {}})
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == "unauthorized"
        r = c.put("/api/automation/policy", json={"policy": {}},
                  headers={"X-Operator-Token": "wrong"})
        assert r.status_code == 401
        r = c.put("/api/automation/policy", json={"policy": {}},
                  headers={"X-Operator-Token": "op-secret-123"})
        assert r.status_code == 200
        # READ stays open without a token
        assert c.get("/api/automation/policy").status_code == 200
        assert c.get("/api/automation/campaigns").status_code == 200


def test_open_local_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    c = _boot(tmp_path, monkeypatch)
    with c:
        assert c.put("/api/automation/policy", json={"policy": {}}).status_code == 200
        assert c.get("/api/automation/meta").json()["auth_mode"] == "open-local"


def test_error_envelope_consistency(tmp_path, monkeypatch):
    c = _boot(tmp_path, monkeypatch)
    with c:
        paths = [c.get("/api/automation/campaigns/nope"),
                 c.post("/api/automation/publish/preflight", json={}).status_code
                 and c.get("/api/automation/submissions/nope"),
                 c.post("/api/automation/providers/qualifications", json={
                     "provider": "x", "platform": "y",
                     "verification_type": "LIVE VERIFIED"})]
        for r in paths:
            assert r.status_code in (400, 404, 409, 422)
            body = r.json()
            assert set(body) == {"error"}, body
            assert set(body["error"]) == {"code", "message", "details"}
            blob = str(body).lower()
            assert "traceback" not in blob and "select " not in blob
            assert "request_id" in body["error"]["details"]


def test_meta_versions_and_diagnostics_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLISH_TOKEN_TIKTOK", "tok-secret-xyz")
    monkeypatch.setenv("SMTP_PASS", "smtp-secret-xyz")
    monkeypatch.setenv("WHOP_API_KEY", "whop-secret-xyz")
    c = _boot(tmp_path, monkeypatch)
    with c:
        meta = c.get("/api/automation/meta").json()
        import version as _version
        assert meta["application_version"] == _version.APP_VERSION
        assert meta["schema_version"] == str(_version.SCHEMA_VERSION)
        diag = c.get("/api/automation/diagnostics/config").json()
        blob = str(diag)
        assert "tok-secret-xyz" not in blob
        assert "smtp-secret-xyz" not in blob
        assert "whop-secret-xyz" not in blob
        assert diag["credentials_present"]["publish_token_tiktok"] is True
        assert c.get("/api/automation/liveness").json()["alive"] is True


def test_request_id_correlation(tmp_path, monkeypatch):
    c = _boot(tmp_path, monkeypatch)
    with c:
        r = c.get("/api/automation/health", headers={"X-Request-ID": "req-test-1"})
        assert r.headers.get("X-Request-ID") == "req-test-1"
        r = c.get("/api/automation/health")
        assert r.headers.get("X-Request-ID", "").startswith("req_")


def test_schema_guard_rejects_newer(tmp_path, monkeypatch):
    _boot(tmp_path, monkeypatch)
    asyncio.run(store.init_automation_tables())
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "lc.db"))
    try:
        con.execute("INSERT INTO automation_meta (key, value) VALUES ('schema_version', '99')"
                    " ON CONFLICT(key) DO UPDATE SET value='99'")
        con.commit()
    finally:
        con.close()
    import pytest
    with pytest.raises(store.IncompatibleSchema):
        asyncio.run(store.ensure_schema_compatible())


def _api_boot(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    db_path = tmp_path / "acc.db"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()
    return TestClient(api.app, raise_server_exceptions=False)


def test_acceptance_recorded_explicitly(tmp_path, monkeypatch):
    import version as _version
    c = _boot(tmp_path, monkeypatch)
    with c:
        assert c.get("/api/automation/acceptance").json()["acceptances"] == []
        r = c.post("/api/automation/acceptance", json={
            "version": _version.APP_VERSION,
            "checklist": {"tests": "pass", "build": "pass"},
            "notes": "operator reviewed RC evidence",
            "actor": "owner"})
        assert r.status_code == 200, r.text
        aid = r.json()["acceptance_id"]
        rows = c.get(
            f"/api/automation/acceptance?version={_version.APP_VERSION}").json()["acceptances"]
        assert any(x["id"] == aid and x["actor"] == "owner" for x in rows)


def test_acceptance_rejects_version_mismatch(tmp_path, monkeypatch):
    c = _api_boot(tmp_path, monkeypatch)
    with c:
        r = c.post("/api/automation/acceptance", json={"version": "9.9.9"})
        assert r.status_code == 400
        assert c.get("/api/automation/acceptance").json()["acceptances"] == []


def test_config_preflight_uses_status_tokens(tmp_path, monkeypatch):
    c = _api_boot(tmp_path, monkeypatch)
    with c:
        cfg = c.get("/api/automation/readiness").json()["config"]
        states = {f["status"] for f in cfg["findings"]}
        assert states <= {"VALID", "INVALID", "PRESENT", "ABSENT", "UNAVAILABLE"}
        creds = [f for f in cfg["findings"]
                 if f["capability"].startswith("credential_")]
        assert creds and all("PRESENT" in f["message"] or "ABSENT" in f["message"]
                             for f in creds)
        for f in creds:
            assert f["status"] in ("PRESENT", "ABSENT")
