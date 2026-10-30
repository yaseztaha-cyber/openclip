"""
API route tests for the editor render job system, editor state endpoints,
render history, and download attachments.

These tests exercise the FastAPI application with a temporary SQLite DB and
tmp dir so they never touch real user data. Heavy renders are NOT executed
here — job lifecycle (queue / cancel / status), state persistence, and
download wiring are covered instead.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

import database
import api


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate DB + tmp storage per test.
    db_path = tmp_path / "test.db"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api.database, "DB_PATH", str(db_path))
    monkeypatch.setattr(api, "TMP_DIR", str(tmp_dir))
    # Reset in-memory job registries.
    api._render_jobs.clear()
    api._render_ws.clear()
    api._latest_render_job.clear()
    with TestClient(api.app) as c:
        yield c, tmp_dir


def _seed_project(tmp_dir):
    asyncio.run(database.init_db())
    proj = asyncio.run(database.create_project("https://youtu.be/abc123"))
    project_id = proj["project_id"]
    asyncio.run(database.update_project_status(project_id, "done"))
    clip_rel = f"/files/{project_id}/clips/clip_seed_1.mp4"
    clipf = os.path.join(tmp_dir, project_id, "clips", "clip_seed_1.mp4")
    os.makedirs(os.path.dirname(clipf), exist_ok=True)
    with open(clipf, "wb") as f:
        f.write(b"fakeclip1")
    src = os.path.join(tmp_dir, project_id, "source.mp4")
    with open(src, "wb") as f:
        f.write(b"fakevp1")
    saved = asyncio.run(database.save_clip(
        project_id=project_id, file_path=clip_rel,
        start_time=1.0, end_time=5.0, title="Seed Clip", reason="test",
        reframed=True))
    return project_id, saved["id"]


def test_editor_state_roundtrip(client):
    c, tmp_dir = client
    project_id, clip_id = _seed_project(tmp_dir)

    state = {
        "source_start": 1.2, "source_end": 4.8, "output_aspect": "9:16",
        "crop_mode": "auto", "zoom": 1.0, "x": 0.5, "y": 0.5,
        "caption_style": "viral_word", "caption_enabled": True,
    }
    r = c.post(f"/api/clips/{clip_id}/editor-state", json=state)
    assert r.status_code == 200, r.text

    r = c.get(f"/api/clips/{clip_id}/editor-state")
    assert r.status_code == 200
    body = r.json()
    assert body["source_start"] == 1.2
    assert body["caption_style"] == "viral_word"

    # Unknown clip -> 404
    assert c.get("/api/clips/nope/editor-state").status_code == 404


def test_editor_render_requires_saved_state(client):
    c, tmp_dir = client
    project_id, clip_id = _seed_project(tmp_dir)
    r = c.post(f"/api/clips/{clip_id}/editor-render")
    assert r.status_code == 400  # "save first"


def test_render_job_idle_and_cancel(client):
    c, tmp_dir = client
    project_id, clip_id = _seed_project(tmp_dir)
    state = {
        "source_start": 1.2, "source_end": 4.8, "output_aspect": "9:16",
        "crop_mode": "manual", "zoom": 1.0, "x": 0.5, "y": 0.5,
        "caption_style": "viral_word", "caption_enabled": False,
    }
    c.post(f"/api/clips/{clip_id}/editor-state", json=state).raise_for_status()

    # idle before any render
    r = c.get(f"/api/clips/{clip_id}/render-job")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"

    # kick off (source file is fake -> render will error; that's fine, we
    # verify the job lifecycle plumbing without ffmpeg).
    r = c.post(f"/api/clips/{clip_id}/editor-render")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"]
    assert body["status"] in ("queued", "running")

    # cancel endpoint exists and is safe even after finish
    r = c.post(f"/api/clips/{clip_id}/render-job/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] in (True, False)


def test_renders_list_and_download_404(client):
    c, tmp_dir = client
    project_id, clip_id = _seed_project(tmp_dir)

    r = c.get(f"/api/clips/{clip_id}/renders")
    assert r.status_code == 200
    assert r.json() == []

    # Download falls back to clip file which exists -> 200
    r = c.get(f"/api/clips/{clip_id}/download")
    assert r.status_code == 200
    assert r.headers.get("content-disposition", "").startswith("attachment")
    assert r.content == b"fakeclip1"

    # Missing clip -> 404
    assert c.get("/api/clips/nope/download").status_code == 404


def test_render_all_requires_project(client):
    c, tmp_dir = client
    assert c.post("/api/projects/nope/render-all").status_code == 404
    # Unknown project → an idle response, never 500.
    r = c.get("/api/projects/nope/render-job")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"