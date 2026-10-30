"""Ops endpoint smoke tests (v1.2): readiness, overview, scheduler ops,
telemetry, alerts, retention, lineage, provider check. Tmp DB only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import database
import api


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "ops_api.db"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    monkeypatch.delenv("PUBLISH_TOKEN_TIKTOK", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()
    return TestClient(api.app, raise_server_exceptions=False), tmp_dir


def test_readiness_separates_app_from_automation(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/api/automation/readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["application"] in ("READY", "DEGRADED")
    assert "matrix" in body and "config" in body
    caps = {m["capability"] for m in body["matrix"]}
    assert {"campaign_discovery", "publishing", "submission_lookup",
            "analytics", "smtp", "mock_simulation"} <= caps
    for m in body["matrix"]:
        for state in ("supported", "configured", "authenticated",
                      "available", "operational"):
            assert isinstance(m[state], bool), (m["capability"], state)


def test_health_marks_optional_unavailable_not_unhealthy(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    body = c.get("/api/automation/health").json()
    assert body["application"] == "HEALTHY"
    assert body["dependencies"]["publisher"]["state"] in (
        "SUPPORTED", "CONFIGURED", "AUTHENTICATED", "AVAILABLE",
        "OPERATIONAL", "UNAVAILABLE")
    assert body["dependencies"]["smtp"]["state"] == "UNAVAILABLE"


def test_overview_scheduler_telemetry_alerts(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    for path in ("/api/automation/overview", "/api/automation/scheduler",
                 "/api/automation/telemetry", "/api/automation/alerts",
                 "/api/automation/providers/capabilities"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text)
    ov = c.get("/api/automation/overview").json()
    assert set(ov) >= {"system", "pipeline", "publication", "analytics"}
    assert ov["publication"]["unknown"] == 0


def test_scheduler_retry_cancel_guards(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    assert c.post("/api/automation/scheduler/jobs/nope/retry").status_code == 404
    # seed a failed local job + a failed publish job directly in store
    import asyncio
    from automation import store
    asyncio.run(store.upsert_job("jl", "render", "failed", "c", "k",
                                 error="boom", payload={"failure_code": "x"}))
    asyncio.run(store.upsert_job("jp", "publish_clip", "failed", "c", "k",
                                 error="boom"))
    r = c.post("/api/automation/scheduler/jobs/jl/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    r = c.post("/api/automation/scheduler/jobs/jp/retry")
    assert r.status_code == 409  # publishing never blind-retried
    r = c.post("/api/automation/scheduler/jobs/jl/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_retention_dry_run_and_lineage(tmp_path, monkeypatch):
    c, tmp_dir = _client(tmp_path, monkeypatch)
    junk = tmp_dir / "clip.seg_0.mp4"
    junk.write_bytes(b"x" * 100)
    r = c.post("/api/automation/retention/cleanup", json={"dry_run": True})
    assert r.status_code == 200
    assert any("seg_0" in x["path"] for x in r.json()["candidates"])
    assert junk.exists()  # dry run deletes nothing
    r = c.get("/api/automation/lineage/unknown-clip")
    assert r.status_code == 200
    chain = r.json()["chain"]
    assert chain["source"] == {"present": False, "clip": None}
    assert chain["approval"]["present"] is False


def test_provider_check_never_publishes(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/automation/providers/check?platform=tiktok")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["published"] is False
    assert body["state"] in ("SUPPORTED", "CONFIGURED", "AUTHENTICATED",
                             "AVAILABLE", "OPERATIONAL", "UNAVAILABLE")
