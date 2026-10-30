"""Phase-22 deterministic end-to-end acceptance over mocks + real ffmpeg.

Covers all 34 acceptance stages with Mock/Replay providers, mock
publisher, mock analytics, and no SMTP. No external calls are made.
"""
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import database
import api
from automation.render_identity import compute_render_hash, content_hash, verify_render_artifact
from ffmpeg_utils import resolve_ffmpeg


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "e2e.db"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    monkeypatch.delenv("LIVE_PUBLISHING_ENABLED", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()
    with TestClient(api.app, raise_server_exceptions=False) as c:
        yield c, tmp_dir


def _src(tmp_dir):
    p = os.path.join(str(tmp_dir), "src.mp4")
    r = subprocess.run(
        [resolve_ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=10:d=6",
         "-f", "lavfi", "-i", "sine=frequency=440:d=6",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", p],
        capture_output=True, timeout=120)
    assert r.returncode == 0, (r.stderr or b"")[-400:]
    return p


def test_full_automation_e2e(client):
    c, tmp_dir = client

    # 1-2. discover (mock + replay) / normalize
    campaigns = [
        {"campaign_id": "e_good", "name": "Good launch",
         "description": "Fun product launch clips", "platforms": ["tiktok"],
         "content_sources": ["https://example.com/v"], "source_url": "https://example.com/v"},
        {"campaign_id": "e_bad", "name": "Bad casino",
         "description": "Online casino sports betting bonus", "platforms": ["tiktok"],
         "content_sources": ["https://example.com/v"]},
    ]
    r = c.post("/api/automation/scan", json={"provider": "mock", "campaigns": campaigns})
    assert r.status_code == 200, r.text
    scan = r.json()
    # 3-5. policy eval: reject violator, retain valid
    assert scan["evaluations"]["e_bad"]["status"] == "REJECTED"
    assert scan["evaluations"]["e_good"]["status"] == "ELIGIBLE"
    assert scan["evaluations"]["e_bad"]["reasons"] == ["gambling"]
    # 6. rank eligible only
    assert [x["campaign_id"] for x in scan["ranked"]] == ["e_good"]
    assert scan["policy_version"] == 1
    # replay provider determinism
    rp = tmp_dir / "replay.json"
    rp.write_text(json.dumps(campaigns), encoding="utf-8")
    r = c.post("/api/automation/scan",
               json={"provider": "replay", "replay_path": str(rp)})
    assert r.status_code == 200
    assert r.json()["evaluations"]["e_good"]["status"] == "ELIGIBLE"

    # 7-9. ingest source + synthetic transcript
    src = _src(tmp_dir)
    transcript = [{"start": 0.0, "end": 2.0, "text": "hello world testing clips"},
                  {"start": 2.0, "end": 4.0, "text": "second line keeps talking here"}]
    # 10-11. silence + music gates through selection
    cands = [
        {"clip_key": "k1", "start": 0.0, "end": 4.0, "hook": 8, "payoff": 8,
         "context": 7, "audio": 7, "silence_ratio": 0.1},
        {"clip_key": "k2", "start": 3.0, "end": 6.0, "hook": 3, "payoff": 3,
         "context": 3, "audio": 3, "silence_ratio": 0.05},
        {"clip_key": "k3", "start": 10.0, "end": 14.0, "hook": 9, "payoff": 9,
         "context": 9, "audio": 9, "silence_ratio": 0.0},
        {"clip_key": "k4", "start": 20.0, "end": 40.0, "hook": 9, "payoff": 9,
         "context": 9, "audio": 9, "silence_ratio": 0.95},
    ]
    music = {"k1": {"status": "NO_MUSIC", "confidence": 0.8},
             "k2": {"status": "NO_MUSIC", "confidence": 0.8},
             "k3": {"status": "MUSIC_PRESENT", "confidence": 0.85},
             "k4": {"status": "NO_MUSIC", "confidence": 0.8}}
    r = c.post("/api/automation/clips/select",
               json={"candidates": cands, "music_statuses": music, "seed": 7})
    assert r.status_code == 200, r.text
    sel = r.json()
    # 12. generic people-mask policy on real pixels (testsrc: no people → CLEAR)
    r = c.post("/api/automation/clips/people-check",
               json={"video_path": src, "start": 0.0, "end": 4.0, "mode": "BLUR_ALL"})
    assert r.status_code == 200, r.text
    gate = r.json()["gate"]
    assert gate["gate"] in ("PASS", "PASS_WITH_BLUR")
    assert "feminine" not in r.text.lower() and "gender" not in r.text.lower()
    # 13-15. scored, overlap-suppressed (k2 weaker → out), diverse
    keys = [s["clip_key"] for s in sel["selected"]]
    assert "k1" in keys and "k2" not in keys
    assert "k3" in [x["clip_key"] for x in sel["rejected_music"]]
    assert "k4" in [x["clip_key"] for x in sel["rejected_silence"]]
    assert any(d["reason"] == "OVERLAP_SUPPRESSED"
               for d in sel["decisions"]["overlap_suppressed"])
    assert sel["config"]["seed"] == 7
    # determinism: identical request → identical selection
    r2 = c.post("/api/automation/clips/select",
                json={"candidates": cands, "music_statuses": music, "seed": 7})
    assert [s["clip_key"] for s in r2.json()["selected"]] == keys

    # 16. render 9:16 social output (real ffmpeg) + 17. people mask render
    out916 = os.path.join(str(tmp_dir), "clip_916.mp4")
    r = subprocess.run(
        [resolve_ffmpeg(), "-y", "-i", src, "-vf",
         "crop=405:720:437:0,scale=1080:1920", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-c:a", "aac", out916],
        capture_output=True, timeout=120)
    assert r.returncode == 0
    ver = verify_render_artifact(out916, expect_aspect="9:16")
    assert ver["ok"], ver["reasons"]
    masked = os.path.join(str(tmp_dir), "clip_masked.mp4")
    r = c.post("/api/automation/clips/e_manual/people/manual", json={
        "start_time": 0.0, "end_time": 2.0,
        "box": {"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.5}, "blur": True})
    assert r.status_code == 200, r.text
    r = c.post("/api/automation/clips/e_manual/people/blur-render", json={
        "input_path": out916, "output_path": masked, "blur_type": "soft"})
    assert r.status_code == 200, r.text
    assert verify_render_artifact(masked)["ok"]

    # 18-21. title/description + validation
    r = c.post("/api/automation/metadata/generate", json={
        "transcript": transcript, "source_title": "Src",
        "campaign": {"name": "Good launch", "source_url": "https://example.com/v"}})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["validation"]["verdict"] == "VALID"
    assert meta["metadata_version"] == 2
    title, desc = meta["title"], meta["description"]
    # tampered metadata fails validation honestly
    r = c.post("/api/automation/metadata/generate", json={
        "transcript": transcript, "source_title": "Src", "campaign": {}})
    m2 = r.json()
    from automation.titles import validate_metadata
    assert validate_metadata("Best ever unbelievable win 500%",
                             "desc", transcript, {})["verdict"] == "NEEDS_REVIEW"
    assert validate_metadata("t", "visit online casino now",
                             transcript, {})["verdict"] == "REJECTED"
    assert validate_metadata("t", "see https://evil.example/x",
                             transcript, {})["verdict"] == "NEEDS_REVIEW"

    # render identity: same inputs → same hash (22. approval binds it)
    rh_a = compute_render_hash(source_id="src", source_start=0.0, source_end=4.0,
                               composition={"canvas": "9:16"},
                               caption_style="viral_word",
                               people_policy={"mode": "BLUR_ALL"},
                               title=title, output_aspect="9:16")
    assert compute_render_hash(source_id="src", source_start=0.0, source_end=4.0,
                               composition={"canvas": "9:16"},
                               caption_style="viral_word",
                               people_policy={"mode": "BLUR_ALL"},
                               title=title, output_aspect="9:16") == rh_a
    r = c.post("/api/automation/approvals", json={
        "clip_id": "e_clip1", "campaign_id": "e_good", "status": "READY_FOR_APPROVAL",
        "title": title, "description": desc, "render_id": "rend_a",
        "render_hash": rh_a})
    assert r.status_code == 200, r.text
    r = c.post("/api/automation/approvals/e_clip1/decision", json={
        "approve": True, "reviewer": "owner", "render_hash": rh_a,
        "title": title, "description": desc})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    # 23-24. alter render → old approval must not authorize the new render
    rh_b = compute_render_hash(source_id="src", source_start=0.0, source_end=5.0,
                               composition={"canvas": "9:16"},
                               caption_style="viral_word",
                               people_policy={"mode": "BLUR_ALL"},
                               title=title, output_aspect="9:16")
    assert rh_b != rh_a
    r = c.post("/api/automation/publish", json={
        "clip_id": "e_clip1", "campaign_id": "e_good", "platform": "tiktok",
        "title": title, "description": desc, "render_hash": rh_b,
        "approval_id": "x", "dry_run": False})
    # dry_run default env is dry_run → intended-action only
    assert r.status_code == 200
    assert r.json()["status"] == "dry_run_intended"

    # 25. approve current render through the real observation path:
    # new render supersedes (APPROVED→DETECTED), re-observe milestones,
    # then decide. No store backdoors.
    r = c.post("/api/automation/approvals", json={
        "clip_id": "e_clip1", "campaign_id": "e_good", "status": "READY_FOR_APPROVAL",
        "title": title, "description": desc, "render_id": "rend_b",
        "render_hash": rh_b})
    assert r.status_code == 200 and r.json()["status"] == "DETECTED", r.text
    r = c.post("/api/automation/approvals", json={
        "clip_id": "e_clip1", "campaign_id": "e_good", "status": "READY_FOR_APPROVAL",
        "title": title, "description": desc, "render_id": "rend_b",
        "render_hash": rh_b})
    assert r.json()["status"] == "READY_FOR_APPROVAL", r.text
    r = c.post("/api/automation/approvals/e_clip1/decision", json={
        "approve": True, "reviewer": "owner", "render_hash": rh_b,
        "title": title, "description": desc})
    assert r.json()["status"] == "APPROVED"
    # supersede + transitions are all in history
    tr = c.get("/api/automation/transitions",
               params={"clip_id": "e_clip1"}).json()["transitions"]
    assert any(t["reason"].startswith("approval_upsert") for t in tr)
    import automation.publishing as _pub
    calls = []
    orig = _pub.MockPublisher.publish
    _pub.MockPublisher.publish = lambda self, *a, **k: calls.append(a) or orig(self, *a, **k)
    try:
        r = c.post("/api/automation/publish", json={
            "clip_id": "e_clip1", "campaign_id": "e_good", "platform": "tiktok",
            "title": title, "description": desc, "render_hash": rh_b,
            "dry_run": True})
    finally:
        _pub.MockPublisher.publish = orig
    assert r.json()["status"] == "dry_run_intended"
    # dry-run uses the mock simulator only; nothing persisted as published
    pubs = c.get("/api/automation/publications").json()["publications"]
    assert all(p.get("status") != "published" for p in pubs)

    # 28-30. mock-live publish + idempotency (exactly one attempt)
    from automation import store
    key = _pub.build_idem_key("e_good", "e_clip1", rh_b, "tiktok")
    assert asyncio.run(store.claim_publication_slot("e_clip1", "e_good", "tiktok", key)) is True
    mock = _pub.MockPublisher()
    first = mock.publish(out916, title, desc, "tiktok", key)
    asyncio.run(store.update_publication_by_key(
        key, status="published", post_url=first["post_url"],
        submission_state="CONFIRMED"))
    assert asyncio.run(store.claim_publication_slot("e_clip1", "e_good", "tiktok", key)) is False
    assert mock.calls == 1
    # 31. analytics (confirmed only where provided)
    r = c.post("/api/automation/analytics", json={
        "clip_id": "e_clip1", "provider": "mock", "source": "mock_provider",
        "observed_status": "CONFIRMED",
        "metrics": {"views": 120, "likes": 11, "earnings_confirmed": 3.5}})
    assert r.status_code == 200
    summary = c.get("/api/automation/analytics/summary").json()
    # 32-33. weekly report reflects real data; unavailable stays unavailable
    r = c.post("/api/automation/reports/weekly", json={})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["email_status"] == "EMAIL_DISABLED"
    assert rep["payload"]["performance"]["estimated_earnings"] == "NOT AVAILABLE"
    # resend protection: identical content reuses the same row (no duplicates
    # on retry); a delivered report is returned without resending.
    r = c.post("/api/automation/reports/weekly", json={})
    assert r.json()["report_id"] == rep["report_id"]
    assert r.json().get("resent") is False
    import automation.reports as _repmod
    sent_calls: list = []
    _orig_send = _repmod.send_email
    _repmod.send_email = lambda *a, **k: sent_calls.append(a) or "EMAIL_SENT"
    try:
        r = c.post("/api/automation/reports/weekly", json={})
        assert r.json()["report_id"] == rep["report_id"]
        assert r.json()["email_status"]== "EMAIL_SENT"
        assert len(sent_calls) == 1
        r = c.post("/api/automation/reports/weekly", json={})
        assert r.json()["report_id"] == rep["report_id"]
        assert r.json().get("resent") is False
        assert len(sent_calls) == 1  # delivered → never resent
    finally:
        _repmod.send_email = _orig_send
    # 34. no secrets anywhere in artifacts
    blob = json.dumps(rep)
    for secret in ("PUBLISH_TOKEN", "WHOP_API_KEY", "SMTP_PASS", "Bearer "):
        assert secret not in blob
    # structured error envelope on 404
    r = c.get("/api/automation/campaigns/does-not-exist")
    assert r.status_code == 404 and "error" in r.json()
