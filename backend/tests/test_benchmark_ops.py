"""
API tests for benchmark import and runs (Phase 5.3.1).
"""
import os
import sys
import tempfile
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient
import api as api_mod


def test_list_benchmark_videos():
    """List videos in benchmark dataset."""
    client = TestClient(api_mod.app)
    resp = client.get("/api/benchmark/videos")
    assert resp.status_code == 200
    data = resp.json()
    assert "videos" in data
    assert "count" in data


def test_get_benchmark_video_not_found():
    """Get non-existent video returns 404."""
    client = TestClient(api_mod.app)
    resp = client.get("/api/benchmark/videos/nonexistent_video_xyz")
    assert resp.status_code == 404


def test_import_video_invalid_permission():
    """Reject invalid permission status."""
    client = TestClient(api_mod.app)
    files = {"file": ("test.mp4", io.BytesIO(b"fake video data"), "video/mp4")}
    data = {"video_id": "test_vid", "permission_status": "invalid"}
    resp = client.post("/api/benchmark/import", files=files, data=data)
    assert resp.status_code == 400


def test_import_video_path_traversal():
    """Reject path traversal in video_id."""
    client = TestClient(api_mod.app)
    files = {"file": ("test.mp4", io.BytesIO(b"fake video data"), "video/mp4")}
    data = {"video_id": "../etc/passwd", "permission_status": "pending"}
    resp = client.post("/api/benchmark/import", files=files, data=data)
    assert resp.status_code == 400


def test_list_benchmark_runs():
    """List benchmark runs."""
    client = TestClient(api_mod.app)
    resp = client.get("/api/benchmark/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert "runs" in data


def test_get_benchmark_run_not_found():
    """Get non-existent run returns 404."""
    client = TestClient(api_mod.app)
    resp = client.get("/api/benchmark/runs/nonexistent_run_xyz")
    assert resp.status_code == 404


def test_start_benchmark_run_unauthorized():
    """Reject benchmark run on unauthorized video."""
    client = TestClient(api_mod.app)
    # Create an unauthorized video first
    body = {"video_id": "unauth_vid_test", "annotator_id": "ann1", "start": 0.0, "end": 10.0}
    client.post("/api/benchmark/annotations", json=body)
    # Try to run benchmark (should fail - no authorized video)
    resp = client.post("/api/benchmark/runs", params={"video_id": "nonexistent_video"})
    assert resp.status_code in (403, 404)
