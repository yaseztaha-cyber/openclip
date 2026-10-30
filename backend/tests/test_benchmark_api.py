"""
API tests for benchmark endpoints (Phase 4.1.2).
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient
import api as api_mod


def test_benchmark_datasets_empty():
    """List datasets when none exist."""
    client = TestClient(api_mod.app)
    resp = client.get("/api/benchmark/datasets")
    assert resp.status_code == 200
    assert "datasets" in resp.json()


def test_create_annotation():
    """Create a benchmark annotation via API."""
    client = TestClient(api_mod.app)
    body = {
        "video_id": "test_video_1",
        "annotator_id": "ann1",
        "start": 10.0,
        "end": 25.0,
        "title": "Great moment",
        "categories": ["strong_opening"],
        "importance": 4,
        "confidence": 5,
    }
    resp = client.post("/api/benchmark/annotations", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "moment_id" in data


def test_create_annotation_invalid_timestamps():
    """Reject invalid timestamps."""
    client = TestClient(api_mod.app)
    body = {
        "video_id": "test_video_1",
        "annotator_id": "ann1",
        "start": 10.0,
        "end": 5.0,  # end < start
    }
    resp = client.post("/api/benchmark/annotations", json=body)
    assert resp.status_code == 400


def test_create_annotation_invalid_importance():
    """Reject invalid importance rating."""
    client = TestClient(api_mod.app)
    body = {
        "video_id": "test_video_1",
        "annotator_id": "ann1",
        "start": 0.0,
        "end": 10.0,
        "importance": 6,  # > 5
    }
    resp = client.post("/api/benchmark/annotations", json=body)
    assert resp.status_code == 400


def test_list_annotations():
    """List annotations for a video."""
    client = TestClient(api_mod.app)
    # Create one first
    body = {
        "video_id": "test_video_list",
        "annotator_id": "ann1",
        "start": 0.0,
        "end": 10.0,
        "title": "Test",
    }
    client.post("/api/benchmark/annotations", json=body)
    resp = client.get("/api/benchmark/annotations/test_video_list")
    assert resp.status_code == 200
    data = resp.json()
    assert "annotations" in data
    assert len(data["annotations"]) >= 1


def test_create_review():
    """Create a candidate review via API."""
    client = TestClient(api_mod.app)
    body = {
        "candidate_id": "cand_000",
        "video_id": "test_video_1",
        "reviewer_id": "rev1",
        "opening_hook": 4,
        "context_completeness": 3,
        "interesting_moment": 5,
        "ending_payoff": 4,
        "audio_clarity": 3,
        "visual_quality": 4,
        "overall_usefulness": 4,
        "label": "Good",
    }
    resp = client.post("/api/benchmark/reviews", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True


def test_list_reviews():
    """List reviews for a video."""
    client = TestClient(api_mod.app)
    body = {
        "candidate_id": "cand_001",
        "video_id": "test_video_reviews",
        "reviewer_id": "rev1",
        "overall_usefulness": 3,
    }
    client.post("/api/benchmark/reviews", json=body)
    resp = client.get("/api/benchmark/reviews/test_video_reviews")
    assert resp.status_code == 200
    data = resp.json()
    assert "reviews" in data


def test_run_match():
    """Run candidate-reference matching via API."""
    client = TestClient(api_mod.app)
    # Create an annotation first
    ann_body = {
        "video_id": "test_video_match",
        "annotator_id": "ann1",
        "start": 0.0,
        "end": 15.0,
        "title": "Moment",
    }
    client.post("/api/benchmark/annotations", json=ann_body)
    # Run match
    match_body = {
        "video_id": "test_video_match",
        "candidates": [{"start": 0.0, "end": 15.0, "title": "cand"}],
        "iou_threshold": 0.5,
    }
    resp = client.post("/api/benchmark/match", json=match_body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "precision" in data
    assert "recall" in data
    assert "f1" in data
    assert data["true_positives"] == 1


def test_match_empty_candidates():
    """Match with empty candidates."""
    client = TestClient(api_mod.app)
    import uuid
    vid = "test_video_empty_match_%s" % uuid.uuid4().hex[:8]
    ann_body = {
        "video_id": vid,
        "annotator_id": "ann1",
        "start": 0.0,
        "end": 15.0,
        "title": "Moment",
    }
    client.post("/api/benchmark/annotations", json=ann_body)
    match_body = {
        "video_id": vid,
        "candidates": [],
        "iou_threshold": 0.5,
    }
    resp = client.post("/api/benchmark/match", json=match_body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["true_positives"] == 0
    assert data["false_negatives"] == 1

def test_path_traversal_blocked():
    client = TestClient(api_mod.app)
    body = {
        "video_id": "../etc/passwd",
        "annotator_id": "ann1",
        "start": 0.0,
        "end": 10.0,
    }
    resp = client.post("/api/benchmark/annotations", json=body)
    assert resp.status_code == 400
