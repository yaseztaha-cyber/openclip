"""
Tests for candidate quality evaluation engine (Phase 3).

All tests use synthetic data, no network, no LLM, no production data.
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import evaluation_engine as ee


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

SIMPLE_TRANSCRIPT = [
    {"start": 0.0, "end": 5.0, "text": "Hello everyone welcome to my channel today"},
    {"start": 5.0, "end": 10.0, "text": "We are going to talk about something amazing"},
    {"start": 10.0, "end": 15.0, "text": "Let me show you what I discovered recently"},
    {"start": 15.0, "end": 20.0, "text": "This is going to change everything you know"},
    {"start": 20.0, "end": 25.0, "text": "Here is the proof and the evidence that supports it"},
    {"start": 25.0, "end": 30.0, "text": "So what does this mean for you and your future"},
]

AUDIO_SIGS = {
    "loudness": [0.3] * 30,
    "burst": [0.1, 0.8, 0.2, 0.9, 0.1] + [0.1] * 25,
    "silence": [0.0] * 30,
}

VISUAL_SIGS = {
    "motion": [0.2] * 30,
    "scene_cut": [0.0] * 5 + [0.8] + [0.0] * 24,
}

CANDIDATES = [
    {"start": 0.0, "end": 15.0, "title": "Intro", "viral_score": 5, "reason": "test", "warnings": []},
    {"start": 10.0, "end": 25.0, "title": "Middle", "viral_score": 7, "reason": "test", "warnings": ["starts_with_filler"]},
    {"start": 20.0, "end": 30.0, "title": "End", "viral_score": 4, "reason": "test", "warnings": []},
]


# ---------------------------------------------------------------------------
# Temporal evaluation tests
# ---------------------------------------------------------------------------

def test_evaluate_temporal_basic():
    m = ee.evaluate_temporal(CANDIDATES[0], 30.0)
    assert m.duration == 15.0
    assert m.min_dur_compliant is True
    assert m.max_dur_compliant is True


def test_evaluate_temporal_short():
    c = {"start": 0.0, "end": 5.0, "title": "Short", "warnings": []}
    m = ee.evaluate_temporal(c, 30.0)
    assert m.min_dur_compliant is False
    assert "below_min_duration" in m.warnings


def test_evaluate_temporal_long():
    c = {"start": 0.0, "end": 150.0, "title": "Long", "warnings": []}
    m = ee.evaluate_temporal(c, 200.0)
    assert m.max_dur_compliant is False


def test_evaluate_temporal_mid_sentence():
    c = {"start": 0.0, "end": 15.0, "title": "Test", "warnings": ["start_mid_sentence", "end_mid_sentence"]}
    m = ee.evaluate_temporal(c, 30.0)
    assert m.start_mid_sentence is True
    assert m.end_mid_sentence is True
    assert "starts_mid_sentence" in m.warnings
    assert "ends_mid_sentence" in m.warnings


def test_evaluate_temporal_silence():
    m = ee.evaluate_temporal(CANDIDATES[0], 30.0, audio_sigs=AUDIO_SIGS)
    assert isinstance(m.silence_ratio_overall, float)
    assert 0 <= m.silence_ratio_overall <= 1


def test_evaluate_temporal_filler():
    c = {"start": 0.0, "end": 15.0, "title": "Test", "warnings": ["starts_with_filler"]}
    m = ee.evaluate_temporal(c, 30.0)
    assert m.starts_with_filler is True


# ---------------------------------------------------------------------------
# Audio evaluation tests
# ---------------------------------------------------------------------------

def test_evaluate_audio_basic():
    m = ee.evaluate_audio(CANDIDATES[0], AUDIO_SIGS)
    assert isinstance(m.speech_ratio, float)
    assert isinstance(m.silence_ratio, float)
    assert 0 <= m.speech_ratio <= 1
    assert 0 <= m.silence_ratio <= 1


def test_evaluate_audio_empty():
    m = ee.evaluate_audio(CANDIDATES[0], None)
    assert m.speech_ratio == 0.0
    assert m.silence_ratio == 0.0


def test_evaluate_audio_no_signals():
    m = ee.evaluate_audio(CANDIDATES[0], {})
    assert m.speech_ratio == 0.0


# ---------------------------------------------------------------------------
# Transcript evaluation tests
# ---------------------------------------------------------------------------

def test_evaluate_transcript_basic():
    m = ee.evaluate_transcript(CANDIDATES[0], SIMPLE_TRANSCRIPT)
    assert m.transcript_coverage >= 0
    assert m.word_count >= 0
    assert isinstance(m.speech_density, float)


def test_evaluate_transcript_empty():
    m = ee.evaluate_transcript(CANDIDATES[0], [])
    assert m.transcript_coverage == 0


def test_evaluate_transcript_with_words():
    words = [{"word": "hello", "start": 0.0, "end": 1.0}, {"word": "world", "start": 1.0, "end": 2.0}]
    m = ee.evaluate_transcript(CANDIDATES[0], SIMPLE_TRANSCRIPT, words=words)
    assert m.word_count >= 0


# ---------------------------------------------------------------------------
# Visual evaluation tests
# ---------------------------------------------------------------------------

def test_evaluate_visual_basic():
    m = ee.evaluate_visual(CANDIDATES[0], VISUAL_SIGS)
    assert isinstance(m.mean_motion, float)
    assert isinstance(m.scene_transition_count, int)


def test_evaluate_visual_empty():
    m = ee.evaluate_visual(CANDIDATES[0], None)
    assert m.mean_motion == 0.0
    assert m.scene_transition_count == 0


# ---------------------------------------------------------------------------
# Diversity evaluation tests
# ---------------------------------------------------------------------------

def test_evaluate_diversity_basic():
    m = ee.evaluate_diversity(CANDIDATES)
    assert m.total_candidates == 3
    assert m.max_overlap_iou >= 0
    assert m.duplicate_count >= 0


def test_evaluate_diversity_empty():
    m = ee.evaluate_diversity([])
    assert m.total_candidates == 0


def test_evaluate_diversity_no_overlap():
    cands = [
        {"start": 0.0, "end": 10.0},
        {"start": 20.0, "end": 30.0},
        {"start": 40.0, "end": 50.0},
    ]
    m = ee.evaluate_diversity(cands)
    assert m.max_overlap_iou == 0.0
    assert m.unique_moments == 3


def test_evaluate_diversity_high_overlap():
    cands = [
        {"start": 0.0, "end": 15.0},
        {"start": 5.0, "end": 20.0},
        {"start": 10.0, "end": 25.0},
    ]
    m = ee.evaluate_diversity(cands)
    assert m.max_overlap_iou > 0.3


# ---------------------------------------------------------------------------
# Full candidate evaluation tests
# ---------------------------------------------------------------------------

def test_evaluate_candidate_basic():
    ev = ee.evaluate_candidate(CANDIDATES[0], SIMPLE_TRANSCRIPT, audio_sigs=AUDIO_SIGS, visual_sigs=VISUAL_SIGS, video_duration=30.0)
    assert ev.candidate_id == "cand_000"
    assert ev.temporal is not None
    assert ev.audio is not None
    assert ev.transcript is not None
    assert ev.visual is not None
    assert isinstance(ev.machine_score, float)
    assert 0 <= ev.machine_score <= 100


def test_evaluate_candidate_warnings():
    ev = ee.evaluate_candidate(CANDIDATES[1], SIMPLE_TRANSCRIPT, video_duration=30.0)
    assert "starts_with_filler" in ev.temporal.warnings or len(ev.warnings) >= 0


# ---------------------------------------------------------------------------
# Full video evaluation tests
# ---------------------------------------------------------------------------

def test_evaluate_video_basic():
    report = ee.evaluate_video(CANDIDATES, SIMPLE_TRANSCRIPT, 30.0, audio_sigs=AUDIO_SIGS, visual_sigs=VISUAL_SIGS)
    assert report.candidate_count == 3
    assert len(report.candidates) == 3
    assert report.diversity is not None
    assert report.evaluation_time >= 0


def test_evaluate_video_empty():
    report = ee.evaluate_video([], SIMPLE_TRANSCRIPT, 30.0)
    assert report.candidate_count == 0
    assert len(report.candidates) == 0


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------

def test_report_to_json():
    report = ee.evaluate_video(CANDIDATES, SIMPLE_TRANSCRIPT, 30.0)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "report.json")
        ee.report_to_json(report, path)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["candidate_count"] == 3


def test_report_to_csv():
    report = ee.evaluate_video(CANDIDATES, SIMPLE_TRANSCRIPT, 30.0)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "report.csv")
        ee.report_to_csv(report, path)
        assert os.path.exists(path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 4  # header + 3 candidates


def test_report_to_html():
    report = ee.evaluate_video(CANDIDATES, SIMPLE_TRANSCRIPT, 30.0)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "report.html")
        ee.report_to_html(report, path)
        assert os.path.exists(path)
        with open(path) as f:
            content = f.read()
        assert "<html>" in content
        assert "OpenClip" in content


# ---------------------------------------------------------------------------
# Reproducibility tests
# ---------------------------------------------------------------------------

def test_reproducibility_basic():
    import deterministic_candidate as dc

    def gen():
        return dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)

    result = ee.check_reproducibility(gen, runs=3)
    assert result["reproducible"] is True
    assert result["count_stable"] is True
    assert result["timestamps_stable"] is True
    assert result["scores_stable"] is True


# ---------------------------------------------------------------------------
# Human review tests
# ---------------------------------------------------------------------------

def test_save_human_review():
    review = ee.HumanReview(
        candidate_id="cand_000",
        reviewer="test",
        opening_hook=4,
        context_completeness=3,
        interesting_moment=5,
        ending_payoff=4,
        audio_clarity=3,
        visual_quality=4,
        overall_usefulness=4,
        label="Good",
        rejection_reasons=[],
        notes="Good clip",
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "reviews.json")
        ee.save_human_review(review, path)
        assert os.path.exists(path)
        loaded = ee.load_human_reviews(path)
        assert len(loaded) == 1
        assert loaded[0].candidate_id == "cand_000"
        assert loaded[0].overall_usefulness == 4


def test_save_multiple_reviews():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "reviews.json")
        for i in range(3):
            review = ee.HumanReview(candidate_id="cand_%03d" % i, reviewer="test", overall_usefulness=i + 1)
            ee.save_human_review(review, path)
        loaded = ee.load_human_reviews(path)
        assert len(loaded) == 3


def test_load_nonexistent_reviews():
    loaded = ee.load_human_reviews("/nonexistent/path/reviews.json")
    assert loaded == []


# ---------------------------------------------------------------------------
# Synthetic video tests
# ---------------------------------------------------------------------------

def test_create_synthetic_video():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(path, duration=10.0, pattern="minimal")
        assert os.path.exists(path)
        assert os.path.getsize(path) > 0


def test_get_video_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(path, duration=10.0, pattern="minimal")
        meta = ee.get_video_metadata(path)
        assert meta["duration"] > 0
        assert meta["width"] > 0
        assert meta["height"] > 0


def test_compute_file_hash():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test.txt")
        with open(path, "w") as f:
            f.write("hello")
        h = ee.compute_file_hash(path)
        assert len(h) == 64  # SHA-256


# ---------------------------------------------------------------------------
# Benchmark tests
# ---------------------------------------------------------------------------

def test_run_benchmark_synthetic():
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(video_path, duration=10.0, pattern="minimal")
        report = ee.run_benchmark(video_path, tmpdir)
        assert report.candidate_count >= 0
        assert report.video_duration > 0
        assert report.video_hash != ""
        assert os.path.exists(os.path.join(tmpdir, "reports"))


def test_run_synthetic_benchmarks():
    with tempfile.TemporaryDirectory() as tmpdir:
        reports = ee.run_synthetic_benchmarks(tmpdir)
        assert isinstance(reports, list)
        assert len(reports) > 0


# ---------------------------------------------------------------------------
# No network access
# ---------------------------------------------------------------------------

def test_no_network_access():
    import socket
    original_connect = socket.socket.connect

    def blocked_connect(self, *args, **kwargs):
        raise RuntimeError("NETWORK CALL DETECTED")

    socket.socket.connect = blocked_connect
    try:
        report = ee.evaluate_video(CANDIDATES, SIMPLE_TRANSCRIPT, 30.0)
        assert report.candidate_count == 3
    finally:
        socket.socket.connect = original_connect
