import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient
import api as api_mod


def test_export_json():
    """Export benchmark report as JSON."""
    client = TestClient(api_mod.app)
    # Create an annotation first
    body = {"video_id": "export_test_v", "annotator_id": "ann1", "start": 0.0, "end": 10.0, "title": "Test"}
    client.post("/api/benchmark/annotations", json=body)
    resp = client.get("/api/benchmark/export/export_test_v?format=json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("content-type", "")
    data = resp.json()
    assert "candidate_count" in data


def test_export_csv():
    """Export benchmark report as CSV."""
    client = TestClient(api_mod.app)
    body = {"video_id": "export_test_csv", "annotator_id": "ann1", "start": 0.0, "end": 10.0, "title": "Test"}
    client.post("/api/benchmark/annotations", json=body)
    resp = client.get("/api/benchmark/export/export_test_csv?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")


def test_export_html():
    """Export benchmark report as HTML."""
    client = TestClient(api_mod.app)
    body = {"video_id": "export_test_html", "annotator_id": "ann1", "start": 0.0, "end": 10.0, "title": "Test"}
    client.post("/api/benchmark/annotations", json=body)
    resp = client.get("/api/benchmark/export/export_test_html?format=html")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "<html>" in resp.text


def test_export_invalid_format():
    """Reject invalid export format."""
    client = TestClient(api_mod.app)
    resp = client.get("/api/benchmark/export/test?format=xml")
    assert resp.status_code == 400
