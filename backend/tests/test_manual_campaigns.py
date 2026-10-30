"""Manual campaigns + stored scan + clip import (real local data, no mocks)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import database
import api


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "real.db"
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
    with TestClient(api.app, raise_server_exceptions=False) as c:
        yield c


def test_manual_campaign_scan_and_delete(tmp_path, monkeypatch):
    for c in _client(tmp_path, monkeypatch):
        r = c.post("/api/automation/campaigns", json={
            "name": "Gadget launch", "description": "Fun clips for launch",
            "platforms": ["tiktok"], "content_sources": ["https://example.com/v"]})
        assert r.status_code == 200, r.text
        cid = r.json()["campaign_id"]
        r = c.post("/api/automation/scan", json={"provider": "stored"})
        assert r.status_code == 200, r.text
        assert r.json()["evaluations"][cid]["status"] == "ELIGIBLE"
        listed = c.get("/api/automation/campaigns").json()["campaigns"]
        assert any(x["campaign_id"] == cid and x["status"] == "ELIGIBLE"
                   for x in listed)
        # gambling campaign is really rejected
        r = c.post("/api/automation/campaigns", json={
            "name": "Casino", "description": "Online casino sports betting",
            "platforms": ["tiktok"], "content_sources": ["https://example.com/v"]})
        bad = r.json()["campaign_id"]
        c.post("/api/automation/scan", json={"provider": "stored"})
        listed = c.get("/api/automation/campaigns?status=REJECTED").json()["campaigns"]
        assert any(x["campaign_id"] == bad for x in listed)
        assert c.delete(f"/api/automation/campaigns/{bad}").status_code == 200
        listed = c.get("/api/automation/campaigns").json()["campaigns"]
        assert all(x["campaign_id"] != bad for x in listed)


def test_import_real_clip_to_approvals(tmp_path, monkeypatch):
    import asyncio
    for c in _client(tmp_path, monkeypatch):
        proj = asyncio.run(database.create_project("https://example.com/v"))
        saved = asyncio.run(database.save_clip(
            proj["project_id"], "/files/x/clip.mp4", 1.0, 9.0,
            title="Real clip", reason="test", viral_score=8,
            face_count=1, layout_mode="single", caption_style="viral_word"))
        clip_id = saved["id"]
        r = c.post("/api/automation/approvals/import",
                   json={"clip_id": clip_id, "campaign_id": "camp1"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "READY_FOR_APPROVAL"
        assert r.json()["detail"]["viral_score"] == 8
        appr = c.get("/api/automation/approvals").json()["approvals"]
        row = next(x for x in appr if x["clip_id"] == clip_id)
        assert row["title"] == "Real clip"
        assert row["preview_path"] == "/files/x/clip.mp4"
        # unknown clip 404s with envelope
        r = c.post("/api/automation/approvals/import", json={"clip_id": "nope"})
        assert r.status_code == 404 and "error" in r.json()
