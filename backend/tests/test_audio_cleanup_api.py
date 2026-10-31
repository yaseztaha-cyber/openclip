"""
API tests for audio cleanup endpoints (Phase 5.3.8).
"""
import os
import sys
import tempfile
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient
import api as api_mod


def test_model_status():
    """Check model status endpoint."""
    client = TestClient(api_mod.app)
    resp = client.get("/api/audio-cleanup/model-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "available" in data
    assert "name" in data


def test_audio_cleanup_inspect():
    """Inspect a video's audio."""
    client = TestClient(api_mod.app)
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        resp = client.post("/api/audio-cleanup/inspect", params={"video_path": vid})
        assert resp.status_code == 200
        data = resp.json()
        assert "duration" in data


def test_audio_cleanup_mute():
    """Mute all audio from a video."""
    client = TestClient(api_mod.app)
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        out = os.path.join(tmpdir, "muted.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        resp = client.post("/api/audio-cleanup/mute", params={"video_path": vid, "output_path": out})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "verified_silent"
        assert os.path.exists(out)


def test_audio_cleanup_review():
    """Review audio without processing."""
    client = TestClient(api_mod.app)
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        resp = client.post("/api/audio-cleanup/review", params={"video_path": vid})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_review"
        assert data["mode"] == "review"


def test_audio_cleanup_remove_music():
    """Attempt to remove music (limited without model)."""
    client = TestClient(api_mod.app)
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        out = os.path.join(tmpdir, "cleaned.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        resp = client.post("/api/audio-cleanup/remove-music", params={"video_path": vid, "output_path": out})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("needs_review", "failed")
        assert data["model_available"] is False
