"""v1.4 release-candidate tests: extended backup verification, disaster
recovery simulation, clean install, access-control classification.
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import database
import api
from automation import store


def _isolate(tmp_path, monkeypatch, name="rc.db"):
    db_path = tmp_path / name
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    for var in ("PUBLISH_TOKEN_TIKTOK", "PUBLISH_TOKEN_YOUTUBE", "WHOP_API_KEY",
                "SMTP_HOST", "SMTP_PASS", "OPERATOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()
    return str(db_path), str(tmp_dir)


def test_backup_verifies_policies_qualifications_reports(tmp_path, monkeypatch):
    db, _tmp = _isolate(tmp_path, monkeypatch)
    asyncio.run(store.init_automation_tables())
    from automation.policy import UserCampaignPolicy, canonical_hash
    p = UserCampaignPolicy()
    asyncio.run(store.save_policy_version(p.to_dict(), canonical_hash(p.to_dict()), "owner"))
    from automation.qualification import new_qualification
    asyncio.run(store.save_qualification(new_qualification(
        provider="mock", platform="tiktok", verification_type="MOCK VERIFIED")))
    asyncio.run(store.save_report("rep_rc1", "2026-01-05", "2026-01-11",
                                  {"week": 1}, "<p>x</p>", "EMAIL_DISABLED",
                                  report_hash="rph_1"))
    backup = str(tmp_path / "backup.db")
    src = sqlite3.connect(db)
    try:
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    database.DB_PATH = backup
    asyncio.run(store.init_automation_tables())
    assert len(asyncio.run(store.list_policy_versions())) == 1
    assert len(asyncio.run(store.list_qualifications())) == 1
    assert len(asyncio.run(store.list_reports())) == 1
    database.DB_PATH = db


def test_disaster_recovery_simulation(tmp_path, monkeypatch):
    db, tmp = _isolate(tmp_path, monkeypatch, "dis.db")
    asyncio.run(store.init_automation_tables())
    asyncio.run(store.upsert_approval("clipD", "cD", status="APPROVED", title="t"))
    asyncio.run(store.claim_publication_slot("clipD", "cD", "tiktok", "DIS_K"))
    asyncio.run(store.update_publication_by_key("DIS_K", status="unknown",
                                                submission_state="UNKNOWN_EXTERNAL_RESULT"))
    art = os.path.join(tmp, "clipD.mp4")
    with open(art, "wb") as fh:
        fh.write(b"fake")
    # backup, then simulate disaster: artifact lost
    backup = str(tmp_path / "dis_backup.db")
    src = sqlite3.connect(db)
    try:
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.remove(art)
    # restore into fresh file; application starts; scheduler recovers safely
    restored = str(tmp_path / "restored.db")
    bcon = sqlite3.connect(backup)
    try:
        rcon = sqlite3.connect(restored)
        try:
            bcon.backup(rcon)
        finally:
            rcon.close()
    finally:
        bcon.close()
    database.DB_PATH = restored
    asyncio.run(store.init_automation_tables())
    pub = asyncio.run(store.find_publication_by_key("DIS_K"))
    assert pub["submission_state"] == "UNKNOWN_EXTERNAL_RESULT"  # unresolved
    assert asyncio.run(store.claim_publication_slot("clipD", "cD", "tiktok", "DIS_K")) is False
    assert (asyncio.run(store.get_approval("clipD")) or {})["status"] == "APPROVED"
    database.DB_PATH = db


def test_clean_install_no_dev_state(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, "clean.db")
    with TestClient(api.app, raise_server_exceptions=False) as c:
        assert c.get("/api/automation/liveness").status_code == 200
        assert c.get("/api/automation/readiness").status_code == 200
        r = c.post("/api/automation/scan", json={"provider": "mock", "campaigns": [
            {"campaign_id": "ci1", "name": "CI",
             "description": "Fun launch", "platforms": ["tiktok"],
             "content_sources": ["https://example.com/v"]}]})
        assert r.status_code == 200, r.text
        assert r.json()["evaluations"]["ci1"]["status"] == "ELIGIBLE"
        r = c.post("/api/automation/publish/preflight", json={
            "clip_id": "ci_clip", "platform": "tiktok", "title": "t",
            "render_hash": "rh"})
        assert r.json()["verdict"] == "BLOCKED"  # no approval, honest
        assert c.get("/api/automation/health").json()["application"] == "HEALTHY"


def test_access_control_classification(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, "acl.db")
    monkeypatch.setenv("OPERATOR_TOKEN", "op-123")
    with TestClient(api.app, raise_server_exceptions=False) as c:
        # READ stays open
        for path in ("/api/automation/policy", "/api/automation/campaigns",
                     "/api/automation/approvals", "/api/automation/health",
                     "/api/automation/readiness", "/api/automation/meta"):
            assert c.get(path).status_code == 200, path
        # LOCAL_MUTATION with external effect requires the token
        gated = [
            ("PUT", "/api/automation/policy", {"policy": {}}),
            ("POST", "/api/automation/publish", {"clip_id": "x", "dry_run": True}),
            ("POST", "/api/automation/approvals/ck/decision", {"approve": False}),
            ("POST", "/api/automation/campaigns/cc/review",
             {"decision": "reject"}),
        ]
        for method, path, body in gated:
            r = c.request(method, path, json=body)
            assert r.status_code == 401, (method, path, r.text)
            r = c.request(method, path, json=body,
                          headers={"X-Operator-Token": "op-123"})
            assert r.status_code != 401, (method, path, r.text)
