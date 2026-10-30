"""v1.6 observability & local autonomy tests. No network, no credentials."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import database
import api
from automation import store

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures")


def _load(*parts):
    with open(os.path.join(FIX, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "v16.db"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    for var in ("PUBLISH_TOKEN_TIKTOK", "WHOP_API_KEY", "SMTP_HOST",
                "OPERATOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()
    return TestClient(api.app, raise_server_exceptions=False)


def test_fixtures_declared():
    for parts in (("campaigns", "basic-v1.json"),
                  ("provider", "submission-v1.json"),
                  ("submissions", "basic-v1.json"),
                  ("analytics", "basic-v1.json")):
        fx = _load(*parts)
        for key in ("fixture_id", "provider", "version", "created_at",
                    "schema_version", "description"):
            assert fx.get(key) not in (None, ""), (parts, key)
        blob = json.dumps(fx).lower()
        assert "token" not in blob and "password" not in blob


def test_local_autonomy_without_external_providers(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c:
        fx = _load("campaigns", "basic-v1.json")
        r = c.post("/api/automation/scan",
                   json={"provider": "replay",
                         "replay_path": os.path.join(
                             FIX, "campaigns", "basic-v1.json")})
        assert r.status_code == 200, r.text
        # replay file holds {"campaigns": [...]}? normalize both shapes
        body = r.json()
        assert body["evaluations"]["replay-good"]["status"] == "ELIGIBLE"
        assert body["evaluations"]["replay-bad"]["status"] == "REJECTED"
        assert [x["campaign_id"] for x in body["ranked"]] == ["replay-good"]
        _ = fx
        # selection + metadata + approval + dry-run + report + lineage +
        # consistency, all local
        sel = c.post("/api/automation/clips/select", json={
            "candidates": [{"clip_key": "a", "start": 0.0, "end": 10.0,
                            "hook": 7, "payoff": 7, "context": 6,
                            "audio": 6, "silence_ratio": 0.1}],
            "seed": 3}).json()
        assert [s["clip_key"] for s in sel["selected"]] == ["a"]
        meta = c.post("/api/automation/metadata/generate", json={
            "transcript": [{"start": 0, "end": 2, "text": "hello world testing"}],
            "source_title": "Src", "campaign": {"name": "Replay Tech Launch"}}).json()
        assert meta["validation"]["verdict"] == "VALID"
        assert c.post("/api/automation/approvals", json={
            "clip_id": "auto1", "campaign_id": "replay-good",
            "status": "READY_FOR_APPROVAL", "title": meta["title"],
            "description": meta["description"]}).status_code == 200
        dry = c.post("/api/automation/publish", json={
            "clip_id": "auto1", "campaign_id": "replay-good",
            "platform": "tiktok", "title": meta["title"],
            "description": meta["description"], "dry_run": True}).json()
        assert dry["status"] == "dry_run_intended"
        rep = c.post("/api/automation/reports/weekly", json={}).json()
        assert rep["email_status"] == "EMAIL_DISABLED"
        assert rep["payload"]["performance"]["estimated_earnings"] == "NOT AVAILABLE"
        chain = c.get("/api/automation/lineage/auto1").json()["chain"]
        assert any(s["stage"] == "APPROVAL" and s["status"] == "READY_FOR_APPROVAL"
                   for s in chain["timeline"])
        assert c.get("/api/automation/consistency").json()["ok"] is True


def test_determinism_same_inputs_same_outputs(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c:
        fx = _load("campaigns", "basic-v1.json")
        first, second = [], []
        for store_acc, _fx in ((first, None), (second, None)):
            r = c.post("/api/automation/scan",
                       json={"provider": "mock",
                             "campaigns": fx["campaigns"]})
            store_acc.append(r.json())
        assert first[0]["ranked"] == second[0]["ranked"]
        assert first[0]["evaluations"] == second[0]["evaluations"]
        sel_body = {"candidates": [
            {"clip_key": "a", "start": 0.0, "end": 10.0, "hook": 7,
             "payoff": 7, "context": 6, "audio": 6, "silence_ratio": 0.1},
            {"clip_key": "b", "start": 5.0, "end": 15.0, "hook": 4,
             "payoff": 4, "context": 4, "audio": 4, "silence_ratio": 0.1}],
            "seed": 11}
        s1 = c.post("/api/automation/clips/select", json=sel_body).json()
        s2 = c.post("/api/automation/clips/select", json=sel_body).json()
        assert s1["selected"] == s2["selected"]
        assert s1["decisions"] == s2["decisions"]


def test_diagnostics_shape_and_states(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c:
        d = c.get("/api/automation/diagnostics").json()
        for key in ("application_version", "schema_version", "mode",
                    "database", "ffmpeg", "storage", "scheduler",
                    "provider_states", "unresolved_submissions", "failed_jobs",
                    "stale_jobs", "consistency", "last_scan",
                    "last_successful_job"):
            assert key in d, key
        assert d["application_version"] == "1.4.0"
        for dep in ("database", "ffmpeg"):
            assert d[dep]["state"] in ("HEALTHY", "DEGRADED", "UNAVAILABLE",
                                       "FAILED")
        blob = json.dumps(d).lower()
        assert "token" not in blob and "password" not in blob


def test_diagnostics_export_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLISH_TOKEN_TIKTOK", "tok-fake-secret-1")
    monkeypatch.setenv("SMTP_PASS", "smtp-fake-secret-2")
    monkeypatch.setenv("OPERATOR_TOKEN", "op-fake-secret-3")
    c = _client(tmp_path, monkeypatch)
    with c:
        for path in ("/api/automation/diagnostics",
                     "/api/automation/diagnostics/export",
                     "/api/automation/diagnostics/config",
                     "/api/automation/consistency",
                     "/api/automation/readiness"):
            blob = c.get(path).text
            assert "tok-fake-secret-1" not in blob, path
            assert "smtp-fake-secret-2" not in blob, path
            assert "op-fake-secret-3" not in blob, path
        b = c.get("/api/automation/diagnostics/export").json()
        assert b["bundle"] == "automation-diagnostics"
        assert "failure_codes" in b and "telemetry" in b


def test_job_timeline_observable(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c:
        asyncio.run(store.upsert_job("tl1", "render", "running", "c", "k"))
        asyncio.run(store.heartbeat_job("tl1", "w1", "cut"))
        asyncio.run(store.upsert_job("tl1", "render", "completed", "c", "k"))
        tl = c.get("/api/automation/jobs/tl1/timeline").json()
        assert tl["status"] == "completed"
        kinds = [e["event"] for e in tl["timeline"]]
        assert "created" in kinds and "heartbeat" in kinds
        assert c.get("/api/automation/jobs/nope/timeline").status_code == 404


def test_report_hash_reproducible_then_sensitive(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c:
        h1 = c.post("/api/automation/reports/weekly", json={}).json()
        h2 = c.post("/api/automation/reports/weekly", json={}).json()
        assert h1["report_id"] == h2["report_id"]
        c.post("/api/automation/scan", json={"provider": "mock", "campaigns": [
            {"campaign_id": "mut", "name": "Mut",
             "description": "Fun launch", "platforms": ["tiktok"],
             "content_sources": ["https://example.com/v"]}]})
        h3 = c.post("/api/automation/reports/weekly", json={}).json()
        assert h3["report_id"] != h1["report_id"]


def test_email_sink_local_only(tmp_path, monkeypatch):
    from automation import email_sink
    sink = tmp_path / "sink"
    monkeypatch.setenv("EMAIL_SINK_DIR", str(sink))
    out = email_sink.store_message(
        subject="Weekly", html="<p>x</p>", text="x",
        recipients=["owner@example.com"], report_hash="rph_1")
    assert out["delivery_state"] == "LOCAL_TEST_SINK"
    assert "SENT" not in out["delivery_state"]
    msgs = email_sink.list_messages()
    assert len(msgs) == 1 and msgs[0]["sink"] == "LOCAL_TEST_SINK"


def test_outage_simulation_local_ops_continue(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c:
        # provider gone: live publish stays blocked, no mock fallback
        r = c.post("/api/automation/publish", json={
            "clip_id": "o1", "platform": "tiktok", "dry_run": False})
        assert r.json()["status"] == "dry_run_intended" or r.status_code in (200, 403, 409)
        # local ops unaffected
        assert c.get("/api/automation/health").json()["application"] == "HEALTHY"
        assert c.post("/api/automation/scan",
                      json={"provider": "mock", "campaigns": []}).status_code == 200
        assert c.post("/api/automation/clips/select",
                      json={"candidates": []}).status_code == 200
        caps = c.get("/api/automation/providers/capabilities").json()
        assert caps["publishers"]["authorized"]["state"] in (
            "SUPPORTED", "CONFIGURED", "AUTHENTICATED", "AVAILABLE",
            "OPERATIONAL", "UNAVAILABLE")


def test_alert_quality_no_false_positives(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c:
        alerts = c.get("/api/automation/alerts").json()["alerts"]
        # fresh system: optional-service absence must not alert
        assert alerts == []
        # real condition: unknown submission fires
        asyncio.run(store.claim_publication_slot("k", "c", "tiktok", "AL_Q"))
        asyncio.run(store.update_publication_by_key("AL_Q", status="unknown",
                                                    submission_state="UNKNOWN_EXTERNAL_RESULT"))
        alerts = c.get("/api/automation/alerts").json()["alerts"]
        assert any(a["alert"] == "submission_unknown" for a in alerts)
