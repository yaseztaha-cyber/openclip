"""
Tests for benchmark dataset and candidate matching (Phase 4).
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import benchmark_dataset as bd


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

SIMPLE_TRANSCRIPT = [
    {"start": 0.0, "end": 5.0, "text": "Hello everyone welcome to my channel today"},
    {"start": 5.0, "end": 10.0, "text": "We are going to talk about something amazing"},
    {"start": 10.0, "end": 15.0, "text": "Let me show you what I discovered recently"},
    {"start": 15.0, "end": 20.0, "text": "This is going to change everything you know"},
]

CANDIDATES = [
    {"start": 0.0, "end": 15.0, "title": "Intro", "viral_score": 5, "warnings": []},
    {"start": 10.0, "end": 25.0, "title": "Middle", "viral_score": 7, "warnings": ["starts_with_filler"]},
    {"start": 20.0, "end": 30.0, "title": "End", "viral_score": 4, "warnings": []},
]


# ---------------------------------------------------------------------------
# Dataset tests
# ---------------------------------------------------------------------------

def test_init_dataset():
    with tempfile.TemporaryDirectory() as tmpdir:
        ddir = bd.init_dataset(tmpdir)
        assert os.path.exists(ddir)
        assert os.path.exists(os.path.join(ddir, "dataset.json"))
        assert os.path.exists(os.path.join(ddir, "annotations"))
        assert os.path.exists(os.path.join(ddir, "reviews"))


def test_add_video():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        meta = bd.VideoMetadata(
            video_id="v1", source_hash="abc123", duration=60.0,
            resolution="1920x1080", fps=30.0, permission_status="authorized",
        )
        bd.add_video(tmpdir, meta)
        videos = bd.get_videos(tmpdir)
        assert len(videos) == 1
        assert videos[0]["video_id"] == "v1"


def test_get_authorized_videos():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        bd.add_video(tmpdir, bd.VideoMetadata(video_id="v1", permission_status="authorized"))
        bd.add_video(tmpdir, bd.VideoMetadata(video_id="v2", permission_status="pending"))
        auth = bd.get_authorized_videos(tmpdir)
        assert len(auth) == 1
        assert auth[0]["video_id"] == "v1"


def test_duplicate_video_update():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        bd.add_video(tmpdir, bd.VideoMetadata(video_id="v1", duration=60.0))
        bd.add_video(tmpdir, bd.VideoMetadata(video_id="v1", duration=120.0))
        videos = bd.get_videos(tmpdir)
        assert len(videos) == 1
        assert videos[0]["duration"] == 120.0


# ---------------------------------------------------------------------------
# Annotation tests
# ---------------------------------------------------------------------------

def test_save_and_load_annotation():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        moment = bd.InterestingMoment(
            moment_id="m1", video_id="v1", start=10.0, end=25.0,
            title="Great moment", categories=["strong_opening"],
            importance=4, confidence=5, annotator_id="ann1",
        )
        bd.save_annotation(tmpdir, moment)
        loaded = bd.load_annotations(tmpdir, "v1")
        assert len(loaded) == 1
        assert loaded[0].moment_id == "m1"
        assert loaded[0].start == 10.0


def test_multiple_annotators():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        m1 = bd.InterestingMoment(moment_id="m1", video_id="v1", start=10.0, end=25.0, annotator_id="ann1")
        m2 = bd.InterestingMoment(moment_id="m2", video_id="v1", start=12.0, end=27.0, annotator_id="ann2")
        bd.save_annotation(tmpdir, m1)
        bd.save_annotation(tmpdir, m2)
        annotators = bd.get_annotators(tmpdir, "v1")
        assert len(annotators) == 2
        all_anns = bd.load_annotations(tmpdir, "v1")
        assert len(all_anns) == 2


def test_annotation_validation():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        # Invalid: end <= start
        try:
            m = bd.InterestingMoment(moment_id="m1", video_id="v1", start=10.0, end=5.0, annotator_id="ann1")
            bd.save_annotation(tmpdir, m)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
    # Invalid: importance out of range
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        try:
            m = bd.InterestingMoment(moment_id="m2", video_id="v1", start=0.0, end=10.0, importance=6, annotator_id="ann1")
            bd.save_annotation(tmpdir, m)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


def test_annotation_preserves_original():
    """Original annotations are preserved, not overwritten."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        m1 = bd.InterestingMoment(moment_id="m1", video_id="v1", start=10.0, end=25.0, annotator_id="ann1")
        bd.save_annotation(tmpdir, m1)
        # Save another moment from same annotator
        m2 = bd.InterestingMoment(moment_id="m2", video_id="v1", start=30.0, end=45.0, annotator_id="ann1")
        bd.save_annotation(tmpdir, m2)
        loaded = bd.load_annotations(tmpdir, "v1", annotator_id="ann1")
        assert len(loaded) == 2


# ---------------------------------------------------------------------------
# Candidate review tests
# ---------------------------------------------------------------------------

def test_save_and_load_review():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        review = bd.CandidateReview(
            review_id="r1", candidate_id="cand_000", video_id="v1",
            reviewer_id="rev1", overall_usefulness=4, label="Good",
        )
        bd.save_candidate_review(tmpdir, review)
        loaded = bd.load_candidate_reviews(tmpdir, "v1")
        assert len(loaded) == 1
        assert loaded[0].candidate_id == "cand_000"


def test_review_validation():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        try:
            review = bd.CandidateReview(
                review_id="r1", candidate_id="cand_000", video_id="v1",
                reviewer_id="rev1", overall_usefulness=6,  # invalid
            )
            bd.save_candidate_review(tmpdir, review)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


def test_review_multiple_reviewers():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        r1 = bd.CandidateReview(review_id="r1", candidate_id="cand_000", video_id="v1", reviewer_id="rev1", overall_usefulness=4)
        r2 = bd.CandidateReview(review_id="r2", candidate_id="cand_000", video_id="v1", reviewer_id="rev2", overall_usefulness=2)
        bd.save_candidate_review(tmpdir, r1)
        bd.save_candidate_review(tmpdir, r2)
        reviewers = bd.get_reviewers(tmpdir, "v1")
        assert len(reviewers) == 2


# ---------------------------------------------------------------------------
# Matching tests
# ---------------------------------------------------------------------------

def test_match_candidates_to_references():
    moments = [
        bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0),
        bd.InterestingMoment(moment_id="m2", video_id="v1", start=20.0, end=30.0),
    ]
    result = bd.match_candidates_to_references(CANDIDATES, moments, iou_threshold=0.5)
    assert result["true_positives"] >= 0
    assert result["false_positives"] >= 0
    assert result["false_negatives"] >= 0
    assert "matches" in result


def test_match_exact_overlap():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    cands = [{"start": 0.0, "end": 15.0, "title": "exact"}]
    result = bd.match_candidates_to_references(cands, moments, iou_threshold=0.5)
    assert result["true_positives"] == 1
    assert result["false_positives"] == 0
    assert result["false_negatives"] == 0


def test_match_no_overlap():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=5.0)]
    cands = [{"start": 20.0, "end": 30.0, "title": "far"}]
    result = bd.match_candidates_to_references(cands, moments, iou_threshold=0.5)
    assert result["true_positives"] == 0
    assert result["false_positives"] == 1
    assert result["false_negatives"] == 1


def test_match_one_to_one():
    """One reference should not match multiple candidates."""
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    cands = [
        {"start": 0.0, "end": 15.0, "title": "c1"},
        {"start": 2.0, "end": 14.0, "title": "c2"},
    ]
    result = bd.match_candidates_to_references(cands, moments, iou_threshold=0.3)
    assert result["true_positives"] == 1  # only one match


def test_match_different_thresholds():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    cands = [{"start": 5.0, "end": 20.0, "title": "partial"}]
    # IoU = 10/20 = 0.5
    r1 = bd.match_candidates_to_references(cands, moments, iou_threshold=0.3)
    r2 = bd.match_candidates_to_references(cands, moments, iou_threshold=0.7)
    assert r1["true_positives"] >= r2["true_positives"]


# ---------------------------------------------------------------------------
# Detection metrics tests
# ---------------------------------------------------------------------------

def test_detection_metrics():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    cands = [{"start": 0.0, "end": 15.0, "title": "exact"}]
    result = bd.match_candidates_to_references(cands, moments, iou_threshold=0.5)
    metrics = bd.compute_detection_metrics(result)
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["f1"] == 1.0


def test_detection_metrics_no_matches():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=5.0)]
    cands = [{"start": 20.0, "end": 30.0, "title": "far"}]
    result = bd.match_candidates_to_references(cands, moments, iou_threshold=0.5)
    metrics = bd.compute_detection_metrics(result)
    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0


# ---------------------------------------------------------------------------
# Ranking metrics tests
# ---------------------------------------------------------------------------

def test_ranking_metrics():
    metrics = bd.compute_ranking_metrics(CANDIDATES, [
        bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0),
    ], k=2, iou_threshold=0.5)
    assert "precision_at_k" in metrics
    assert "recall_at_k" in metrics
    assert "ndcg_at_k" in metrics
    assert "average_precision" in metrics


def test_boundary_metrics():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    cands = [{"start": 1.0, "end": 14.0, "title": "close"}]
    result = bd.match_candidates_to_references(cands, moments, iou_threshold=0.3)
    bm = bd.compute_boundary_metrics(result)
    assert bm["mean_start_error"] == 1.0
    assert bm["mean_end_error"] == 1.0


def test_boundary_tolerance():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    cands = [{"start": 0.5, "end": 14.5, "title": "close"}]
    result = bd.match_candidates_to_references(cands, moments, iou_threshold=0.3)
    bt = bd.compute_boundary_tolerance(result, tolerance=1.0)
    assert bt["success_rate"] == 1.0


# ---------------------------------------------------------------------------
# Ablation tests
# ---------------------------------------------------------------------------

def test_run_ablation():
    audio = {"loudness": [0.3] * 30, "burst": [0.1] * 30, "silence": [0.0] * 30}
    visual = {"motion": [0.2] * 30, "scene_cut": [0.0] * 30}
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    result = bd.run_ablation(SIMPLE_TRANSCRIPT, 30.0, audio_sigs=audio, visual_sigs=visual, references=moments)
    assert "transcript_only" in result
    assert "audio_only" in result
    assert "visual_only" in result
    assert "full" in result
    for name, data in result.items():
        assert "candidate_count" in data


# ---------------------------------------------------------------------------
# Agreement tests
# ---------------------------------------------------------------------------

def test_boundary_agreement_single():
    result = bd.compute_boundary_agreement([[]])
    assert result["status"] == "insufficient_annotators"


def test_boundary_agreement_two():
    m1 = bd.InterestingMoment(moment_id="m1", video_id="v1", start=10.0, end=25.0)
    m2 = bd.InterestingMoment(moment_id="m2", video_id="v1", start=12.0, end=27.0)
    result = bd.compute_boundary_agreement([[m1], [m2]])
    assert result["status"] == "ok"
    assert result["matched_pairs"] == 1


def test_importance_agreement():
    m1 = bd.InterestingMoment(moment_id="m1", video_id="v1", start=10.0, end=25.0, importance=4)
    m2 = bd.InterestingMoment(moment_id="m2", video_id="v1", start=12.0, end=27.0, importance=3)
    result = bd.compute_importance_agreement([[m1], [m2]])
    assert result["status"] == "ok"
    assert result["mean_importance_diff"] == 1.0


def test_category_agreement():
    m1 = bd.InterestingMoment(moment_id="m1", video_id="v1", start=10.0, end=25.0, categories=["strong_opening"])
    m2 = bd.InterestingMoment(moment_id="m2", video_id="v1", start=12.0, end=27.0, categories=["strong_opening", "funny_moment"])
    result = bd.compute_category_agreement([[m1], [m2]])
    assert result["status"] == "ok"
    assert result["mean_category_jaccard"] > 0


# ---------------------------------------------------------------------------
# Ranking weakness analysis tests
# ---------------------------------------------------------------------------

def test_analyze_ranking_weaknesses():
    moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
    result = bd.analyze_ranking_weaknesses(CANDIDATES, moments)
    assert "total_candidates" in result
    assert "precision" in result
    assert "recall" in result


# ---------------------------------------------------------------------------
# Improve ranking tests
# ---------------------------------------------------------------------------

def test_improve_ranking():
    improved = bd.improve_ranking(CANDIDATES, SIMPLE_TRANSCRIPT)
    assert len(improved) == len(CANDIDATES)
    for c in improved:
        assert "_improved_score" in c
        assert "viral_score" in c
        assert 1 <= c["viral_score"] <= 10


def test_improve_ranking_empty():
    result = bd.improve_ranking([], SIMPLE_TRANSCRIPT)
    assert result == []


# ---------------------------------------------------------------------------
# No network access
# ---------------------------------------------------------------------------

def test_no_network():
    import socket
    original = socket.socket.connect

    def blocked(self, *args, **kwargs):
        raise RuntimeError("NETWORK")

    socket.socket.connect = blocked
    try:
        bd.run_ablation(SIMPLE_TRANSCRIPT, 30.0)
        moments = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=0.0, end=15.0)]
        bd.match_candidates_to_references(CANDIDATES, moments)
        bd.compute_detection_metrics(bd.match_candidates_to_references(CANDIDATES, moments))
    finally:
        socket.socket.connect = original
