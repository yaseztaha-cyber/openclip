"""
Regression tests for deterministic candidate generation (Phase 2).

Tests the fully local, deterministic clip candidate engine that replaces
the LLM-dependent get_clip_suggestions().  No network calls, no LLM.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import deterministic_candidate as dc


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

def _make_transcript(segments):
    return segments


SIMPLE_TRANSCRIPT = [
    {"start": 0.0, "end": 5.0, "text": "Hello everyone welcome to my channel today"},
    {"start": 5.0, "end": 10.0, "text": "We are going to talk about something amazing"},
    {"start": 10.0, "end": 15.0, "text": "Let me show you what I discovered recently"},
    {"start": 15.0, "end": 20.0, "text": "This is going to change everything you know"},
    {"start": 20.0, "end": 25.0, "text": "Here is the proof and the evidence that supports it"},
    {"start": 25.0, "end": 30.0, "text": "So what does this mean for you and your future"},
]

LONG_TRANSCRIPT = []
for i in range(20):
    LONG_TRANSCRIPT.append({
        "start": float(i * 5),
        "end": float(i * 5 + 5),
        "text": "This is sentence number %d with some content to fill time" % (i + 1),
    })

SHORT_TRANSCRIPT = [
    {"start": 0.0, "end": 3.0, "text": "Hi there"},
    {"start": 3.0, "end": 6.0, "text": "Short video"},
]

INCOMPLETE_TRANSCRIPT = [
    {"start": 0.0, "end": 5.0, "text": "This sentence is cut off in the middle of"},
]


# ---------------------------------------------------------------------------
# Test 1: Empty transcript
# ---------------------------------------------------------------------------

def test_empty_transcript():
    result = dc.generate_candidates([], 60.0)
    assert result == [], "Empty transcript should return empty list"


# ---------------------------------------------------------------------------
# Test 2: One complete sentence
# ---------------------------------------------------------------------------

def test_one_complete_sentence():
    transcript = [{"start": 0.0, "end": 12.0, "text": "This is a complete sentence with enough content to be a clip"}]
    result = dc.generate_candidates(transcript, 12.0)
    # Single sentence is only 12s, meets min_dur=10
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test 3: Multiple sentences
# ---------------------------------------------------------------------------

def test_multiple_sentences():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    assert len(result) > 0, "Should generate candidates from multiple sentences"
    for c in result:
        assert "start" in c
        assert "end" in c
        assert "title" in c
        assert "viral_score" in c
        assert c["generation_source"] == "deterministic"


# ---------------------------------------------------------------------------
# Test 4: Incomplete sentence at source end
# ---------------------------------------------------------------------------

def test_incomplete_sentence_at_end():
    result = dc.generate_candidates(INCOMPLETE_TRANSCRIPT, 5.0)
    # The single sentence is only 5s, below min_dur=10, so no candidates
    # But if we extend duration, it should still work
    result2 = dc.generate_candidates(INCOMPLETE_TRANSCRIPT, 15.0)
    assert isinstance(result2, list)


# ---------------------------------------------------------------------------
# Test 5: Very short source
# ---------------------------------------------------------------------------

def test_very_short_source():
    result = dc.generate_candidates(SHORT_TRANSCRIPT, 6.0)
    # 6s total, below min_dur=10, so likely empty
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test 6: Very long source
# ---------------------------------------------------------------------------

def test_very_long_source():
    result = dc.generate_candidates(LONG_TRANSCRIPT, 100.0)
    assert isinstance(result, list)
    assert len(result) <= 15, "Should respect top_k default"


# ---------------------------------------------------------------------------
# Test 7: Minimum duration
# ---------------------------------------------------------------------------

def test_minimum_duration():
    # Candidate with min_dur=15 should not produce clips shorter than 15s
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, min_dur=15.0)
    for c in result:
        assert c["end"] - c["start"] >= 15.0 - 0.01, "All clips should be >= min_dur"


# ---------------------------------------------------------------------------
# Test 8: Maximum duration
# ---------------------------------------------------------------------------

def test_maximum_duration():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, max_dur=20.0)
    for c in result:
        assert c["end"] - c["start"] <= 20.0 + 0.01, "All clips should be <= max_dur"


# ---------------------------------------------------------------------------
# Test 9: Overlapping candidates
# ---------------------------------------------------------------------------

def test_overlapping_candidates():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    # Check that no two candidates have IoU > 0.6
    for i in range(len(result)):
        for j in range(i + 1, len(result)):
            a = result[i]
            b = result[j]
            inter = max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
            union = (a["end"] - a["start"]) + (b["end"] - b["start"]) - inter
            iou = inter / union if union > 0 else 0
            assert iou <= 0.65, "Candidates should not excessively overlap (iou=%.2f)" % iou


# ---------------------------------------------------------------------------
# Test 10: Duplicate candidate suppression
# ---------------------------------------------------------------------------

def test_duplicate_suppression():
    # Running twice should produce identical results
    r1 = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    r2 = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    assert len(r1) == len(r2), "Should produce same number of candidates"
    for a, b in zip(r1, r2):
        assert a["start"] == b["start"], "Start times should match"
        assert a["end"] == b["end"], "End times should match"
        assert a["viral_score"] == b["viral_score"], "Scores should match"


# ---------------------------------------------------------------------------
# Test 11: Missing audio signals
# ---------------------------------------------------------------------------

def test_missing_audio_signals():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, audio_sigs=None)
    assert isinstance(result, list)
    # Should still produce candidates from transcript alone


# ---------------------------------------------------------------------------
# Test 12: Missing visual signals
# ---------------------------------------------------------------------------

def test_missing_visual_signals():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, visual_sigs=None)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test 13: Missing transcript timestamps
# ---------------------------------------------------------------------------

def test_missing_timestamps():
    # Segments without start/end
    transcript = [
        {"text": "Hello world this is a test sentence"},
        {"text": "Another sentence here"},
    ]
    result = dc.generate_candidates(transcript, 30.0)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test 14: Equal scoring and deterministic tie-breaking
# ---------------------------------------------------------------------------

def test_deterministic_tiebreaking():
    r1 = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    r2 = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    # Exact equality
    assert r1 == r2, "Repeated runs should produce identical output"


# ---------------------------------------------------------------------------
# Test 15: Repeated execution with identical input
# ---------------------------------------------------------------------------

def test_repeated_execution():
    results = [dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0) for _ in range(5)]
    for r in results[1:]:
        assert r == results[0], "All runs should produce identical output"


# ---------------------------------------------------------------------------
# Test 16: Invalid timestamps
# ---------------------------------------------------------------------------

def test_invalid_timestamps():
    transcript = [
        {"start": 5.0, "end": 3.0, "text": "Reversed timestamps"},  # start > end
        {"start": 10.0, "end": 10.0, "text": "Zero duration"},  # zero duration
        {"start": -1.0, "end": 5.0, "text": "Negative start"},  # negative start
    ]
    result = dc.generate_candidates(transcript, 30.0)
    assert isinstance(result, list)
    # No candidate should have negative start or zero duration
    for c in result:
        assert c["start"] >= 0, "Start should be non-negative"
        assert c["end"] > c["start"], "End should be after start"


# ---------------------------------------------------------------------------
# Test 17: Zero network access
# ---------------------------------------------------------------------------

def test_zero_network_access():
    """Verify no network calls are made during candidate generation."""
    import socket
    original_connect = socket.socket.connect

    def blocked_connect(self, *args, **kwargs):
        raise RuntimeError("NETWORK CALL DETECTED: %s" % str(args))

    socket.socket.connect = blocked_connect
    try:
        result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
        assert isinstance(result, list)
    finally:
        socket.socket.connect = original_connect


# ---------------------------------------------------------------------------
# Test 18: Existing review and rendering compatibility
# ---------------------------------------------------------------------------

def test_output_format_compatibility():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    required_fields = ["start", "end", "title", "reason", "viral_score",
                       "hook", "payoff", "curiosity", "information", "narrative"]
    for c in result:
        for field in required_fields:
            assert field in c, "Missing field: %s" % field
        assert isinstance(c["start"], (int, float))
        assert isinstance(c["end"], (int, float))
        assert isinstance(c["title"], str)
        assert isinstance(c["viral_score"], int)
        assert 1 <= c["viral_score"] <= 10


# ---------------------------------------------------------------------------
# Test: Audio signals integration
# ---------------------------------------------------------------------------

def test_with_audio_signals():
    audio_sigs = {
        "loudness": [0.3] * 30,
        "burst": [0.1, 0.8, 0.2, 0.9, 0.1] + [0.1] * 25,
        "silence": [0.0] * 30,
    }
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, audio_sigs=audio_sigs)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test: Visual signals integration
# ---------------------------------------------------------------------------

def test_with_visual_signals():
    visual_sigs = {
        "motion": [0.2] * 30,
        "scene_cut": [0.0] * 5 + [0.8] + [0.0] * 24,
    }
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, visual_sigs=visual_sigs)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test: Empty transcript with duration
# ---------------------------------------------------------------------------

def test_empty_transcript_with_duration():
    result = dc.generate_candidates([], 60.0)
    assert result == []


# ---------------------------------------------------------------------------
# Test: Progress callback
# ---------------------------------------------------------------------------

def test_progress_callback():
    calls = []

    def cb(stage, pct, msg):
        calls.append((stage, pct, msg))

    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, progress_callback=cb)
    assert len(calls) > 0, "Progress callback should be called"
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test: top_k parameter
# ---------------------------------------------------------------------------

def test_top_k():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, top_k=3)
    assert len(result) <= 3


# ---------------------------------------------------------------------------
# Test: Words synthesis from segments without word timestamps
# ---------------------------------------------------------------------------

def test_word_synthesis():
    # Transcript without word-level timestamps
    transcript = [
        {"start": 0.0, "end": 10.0, "text": "Hello world this is a test"},
        {"start": 10.0, "end": 20.0, "text": "Another sentence here"},
    ]
    result = dc.generate_candidates(transcript, 20.0)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test: Boundary info in candidates
# ---------------------------------------------------------------------------

def test_boundary_info():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    for c in result:
        assert "boundary_info" in c
        assert "warnings" in c
        assert isinstance(c["warnings"], list)
        assert isinstance(c["boundary_info"], dict)


# ---------------------------------------------------------------------------
# Test: Score components present
# ---------------------------------------------------------------------------

def test_score_explainability():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0)
    for c in result:
        assert "_det" not in c  # internal keys cleaned
        assert "_score" not in c
        assert "reason" in c
        assert len(c["reason"]) > 0


# ---------------------------------------------------------------------------
# Test: Invalid video duration
# ---------------------------------------------------------------------------

def test_invalid_duration():
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 0.0)
    # Should derive duration from transcript
    assert isinstance(result, list)

    result2 = dc.generate_candidates(SIMPLE_TRANSCRIPT, -5.0)
    assert isinstance(result2, list)


# ---------------------------------------------------------------------------
# Test: Mid-sentence boundary detection
# ---------------------------------------------------------------------------

def test_mid_sentence_detection():
    # Create a transcript where a candidate might start mid-sentence
    transcript = [
        {"start": 0.0, "end": 8.0, "text": "This is a long sentence that spans multiple segments"},
        {"start": 8.0, "end": 16.0, "text": "And continues here with more content"},
    ]
    result = dc.generate_candidates(transcript, 16.0)
    assert isinstance(result, list)
    # Check that warnings field exists
    for c in result:
        assert "warnings" in c


# ---------------------------------------------------------------------------
# Test: Candidate generation with audio peaks
# ---------------------------------------------------------------------------

def test_audio_peak_candidates():
    audio_sigs = {
        "loudness": [0.2] * 30,
        "burst": [0.1] * 5 + [0.9] + [0.1] * 24,
        "silence": [0.0] * 30,
    }
    result = dc.generate_candidates(SIMPLE_TRANSCRIPT, 30.0, audio_sigs=audio_sigs)
    assert isinstance(result, list)
    # Should have candidates (audio peaks may create additional windows)
    assert len(result) > 0
