"""
Tests for failure analysis module (Phase 5.1).
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import failure_analysis as fa
import benchmark_dataset as bd


def test_analyze_false_positives():
    cands = [{"start": 0.0, "end": 15.0, "title": "FP", "warnings": ["starts_with_filler"]}]
    refs = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=20.0, end=35.0)]
    match = bd.match_candidates_to_references(cands, refs, 0.5)
    records = fa.analyze_false_positives(cands, refs, match)
    assert len(records) == 1
    assert records[0].failure_type == "false_positive"
    assert records[0].likely_cause == "starts_with_filler" or records[0].likely_cause != ""


def test_analyze_false_negatives():
    cands = [{"start": 0.0, "end": 15.0, "title": "FP"}]
    refs = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=20.0, end=35.0)]
    match = bd.match_candidates_to_references(cands, refs, 0.5)
    records = fa.analyze_false_negatives(cands, refs, match)
    assert len(records) == 1
    assert records[0].failure_type == "false_negative"


def test_analyze_failure_patterns():
    records = [
        fa.FailureRecord(failure_type="false_positive", likely_cause="starts_mid_sentence"),
        fa.FailureRecord(failure_type="false_positive", likely_cause="starts_mid_sentence"),
        fa.FailureRecord(failure_type="false_negative", likely_cause="no_candidate_generated"),
    ]
    patterns = fa.analyze_failure_patterns(records)
    assert patterns["total_fp"] == 2
    assert patterns["total_fn"] == 1
    assert patterns["false_positive_causes"]["starts_mid_sentence"] == 2


def test_generate_failure_report():
    cands = [{"start": 0.0, "end": 15.0, "title": "Test", "warnings": []}]
    refs = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=20.0, end=35.0)]
    match = bd.match_candidates_to_references(cands, refs, 0.5)
    report = fa.generate_failure_report("v1", cands, refs, match, dataset_version="v1", experiment_id="exp1")
    assert report.video_id == "v1"
    assert report.false_positives == 1
    assert report.false_negatives == 1
    assert len(report.false_positive_records) == 1
    assert len(report.false_negative_records) == 1


def test_save_failure_report():
    cands = [{"start": 0.0, "end": 15.0, "title": "Test", "warnings": []}]
    refs = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=20.0, end=35.0)]
    match = bd.match_candidates_to_references(cands, refs, 0.5)
    report = fa.generate_failure_report("v1", cands, refs, match)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "failure.json")
        fa.save_failure_report(report, path)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["video_id"] == "v1"


def test_no_network():
    import socket
    original = socket.socket.connect
    def blocked(self, *args, **kwargs):
        raise RuntimeError("NETWORK")
    socket.socket.connect = blocked
    try:
        cands = [{"start": 0.0, "end": 15.0, "title": "Test", "warnings": []}]
        refs = [bd.InterestingMoment(moment_id="m1", video_id="v1", start=20.0, end=35.0)]
        match = bd.match_candidates_to_references(cands, refs, 0.5)
        report = fa.generate_failure_report("v1", cands, refs, match)
        assert report is not None
    finally:
        socket.socket.connect = original
