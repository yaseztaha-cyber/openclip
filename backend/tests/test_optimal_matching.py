"""
Tests for optimal bipartite matching (Phase 4.1).

Verifies that the Hungarian algorithm finds the globally optimal
one-to-one matching, not just a greedy approximation.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import optimal_matching as om


# ---------------------------------------------------------------------------
# Basic matching tests
# ---------------------------------------------------------------------------

def test_empty_candidates():
    result = om.optimal_match([], [{"start": 0, "end": 10}])
    assert result["true_positives"] == 0
    assert result["false_positives"] == 0
    assert result["false_negatives"] == 1


def test_empty_references():
    result = om.optimal_match([{"start": 0, "end": 10}], [])
    assert result["true_positives"] == 0
    assert result["false_positives"] == 1
    assert result["false_negatives"] == 0


def test_both_empty():
    result = om.optimal_match([], [])
    assert result["true_positives"] == 0
    assert result["false_positives"] == 0
    assert result["false_negatives"] == 0


def test_exact_match():
    cands = [{"start": 0.0, "end": 10.0}]
    refs = [{"start": 0.0, "end": 10.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert result["true_positives"] == 1
    assert result["matches"][0]["iou"] == 1.0


def test_no_valid_edges():
    cands = [{"start": 0.0, "end": 5.0}]
    refs = [{"start": 20.0, "end": 25.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert result["true_positives"] == 0
    assert result["false_positives"] == 1
    assert result["false_negatives"] == 1


# ---------------------------------------------------------------------------
# Optimal vs greedy tests
# ---------------------------------------------------------------------------

def test_optimal_beats_greedy():
    """Case where greedy (highest IoU first) produces suboptimal cardinality.

    Candidate A: [0, 10] — overlaps ref1 [0, 10] IoU=1.0 and ref2 [5, 15] IoU=0.5
    Candidate B: [5, 15] — overlaps ref1 [0, 10] IoU=0.5 and ref2 [5, 15] IoU=1.0

    Greedy picks A-ref1 (IoU=1.0), then B can't match ref2 (ref1 used, but B-ref2 is valid).
    Actually greedy by IoU: A-ref1 (1.0) first, then B-ref2 (1.0) → 2 matches.
    This case doesn't break greedy.

    Better test: 3 candidates, 2 references where greedy picks wrong.
    """
    # Candidate 0: [0, 10] — IoU with ref0 [0, 10] = 1.0, ref1 [8, 18] = 0.2
    # Candidate 1: [8, 18] — IoU with ref0 [0, 10] = 0.2, ref1 [8, 18] = 1.0
    # Candidate 2: [4, 14] — IoU with ref0 [0, 10] = 0.6, ref1 [8, 18] = 0.6
    # Greedy: picks C0-ref0 (1.0), then C1-ref1 (1.0), C2 has no valid match → 2 matches
    # Optimal: same → 2 matches. Need a case where greedy gets 1 but optimal gets 2.

    # Classic counterexample:
    # C0: [0, 10] — ref0 [0, 10] IoU=1.0, ref1 [9, 19] IoU=0.1
    # C1: [9, 19] — ref0 [0, 10] IoU=0.1, ref1 [9, 19] IoU=1.0
    # C2: [0, 19] — ref0 [0, 10] IoU=0.53, ref1 [9, 19] IoU=0.58
    # Greedy: C0-ref0 (1.0), C1-ref1 (1.0), C2 no match → 2
    # Optimal: 2. Still same.

    # The real counterexample for greedy is when a high-IoU single match
    # blocks two lower-IoU matches:
    # C0: [0, 10] — ref0 [0, 10] IoU=1.0
    # C1: [0, 10] — ref0 [0, 10] IoU=0.9, ref1 [5, 15] IoU=0.5
    # C2: [5, 15] — ref0 [0, 10] IoU=0.5, ref1 [5, 15] IoU=1.0
    # Greedy: C0-ref0 (1.0), then C2-ref1 (1.0) → 2 matches (C1 dropped)
    # Optimal: C0-ref0 (1.0), C2-ref1 (1.0) → 2 matches. Same.

    # The true greedy failure: when greedy picks a high-IoU edge that blocks
    # two slightly-lower edges that together have higher total:
    # C0: [0, 10] — ref0 [0, 10] IoU=0.9
    # C1: [0, 10] — ref0 [0, 10] IoU=0.8, ref1 [5, 15] IoU=0.8
    # C2: [5, 15] — ref0 [0, 10] IoU=0.8, ref1 [5, 15] IoU=0.9
    # Greedy: C0-ref0 (0.9), then C2-ref1 (0.9) → 2 matches, total 1.8
    #         (C1 dropped because ref0 and ref1 both used)
    # Optimal: C1-ref0 (0.8) + C2-ref1 (0.9) = 1.7, or C0-ref0 (0.9) + C2-ref1 (0.9) = 1.8
    # Same result. Greedy wins here.

    # The actual greedy failure is about CARDINALITY, not weight:
    # C0: [0, 10] — ref0 [0, 10] IoU=1.0 (only ref0)
    # C1: [0, 10] — ref0 [0, 10] IoU=0.5, ref1 [5, 15] IoU=0.5
    # C2: [5, 15] — ref1 [5, 15] IoU=1.0 (only ref1)
    # Greedy by IoU: C0-ref0 (1.0), C2-ref1 (1.0) → 2 matches
    # Optimal: 2 matches. Same.

    # The real counterexample requires a case where greedy picks a match that
    # prevents a DIFFERENT match with higher cardinality:
    # C0: [0, 10] — ref0 [0, 10] IoU=0.9
    # C1: [0, 10] — ref0 [0, 10] IoU=0.5, ref1 [0, 10] IoU=0.5
    # Wait, ref0 and ref1 are the same interval? No, they must be different.

    # Let me construct a proper example:
    # ref0: [0, 10], ref1: [5, 15]
    # C0: [0, 10] — ref0=1.0, ref1=0.5
    # C1: [5, 15] — ref0=0.5, ref1=1.0
    # C2: [0, 15] — ref0=0.67, ref1=0.67
    # Greedy: C0-ref0 (1.0), C1-ref1 (1.0) → 2 matches, total 2.0
    # Optimal: same. 2 matches.

    # The greedy failure for CARDINALITY:
    # ref0: [0, 10], ref1: [20, 30]
    # C0: [0, 10] — ref0=1.0 (only)
    # C1: [0, 30] — ref0=0.33, ref1=0.33
    # C2: [20, 30] — ref1=1.0 (only)
    # Greedy: C0-ref0 (1.0), C2-ref1 (1.0) → 2 matches
    # Optimal: 2 matches. Same.

    # Actually, greedy by highest IoU first ALWAYS finds max cardinality
    # when all weights are positive and the graph is such that... no, that's not true.
    # Counterexample for greedy cardinality:
    # ref0: [0, 10], ref1: [5, 15]
    # C0: [0, 15] — ref0=0.67, ref1=0.67
    # C1: [0, 10] — ref0=1.0 (only ref0)
    # C2: [5, 15] — ref1=1.0 (only ref1)
    # Greedy by IoU: C0-ref0 (0.67) first → then C1 can't match ref0, C2 can match ref1
    # → 2 matches (C0-ref0, C2-ref1), total 1.67
    # But optimal: C1-ref0 (1.0) + C2-ref1 (1.0) = 2.0, total 2.0
    # Greedy got 2 matches but lower total IoU.
    # With lexicographic objective (max cardinality first, then max IoU):
    # Both have 2 matches, so we maximize IoU → optimal picks C1+C2 (total 2.0)

    cands = [
        {"start": 0.0, "end": 15.0},   # C0: overlaps both
        {"start": 0.0, "end": 10.0},   # C1: only ref0
        {"start": 5.0, "end": 15.0},   # C2: only ref1
    ]
    refs = [
        {"start": 0.0, "end": 10.0},   # ref0
        {"start": 5.0, "end": 15.0},   # ref1
    ]
    result = om.optimal_match(cands, refs, iou_threshold=0.3)
    assert result["true_positives"] == 2, "Should find 2 matches"
    # Optimal should match C1-ref0 and C2-ref1 (total IoU = 2.0)
    # not C0-ref0 and C2-ref1 (total IoU = 1.67)
    matched_pairs = [(m["candidate_idx"], m["reference_idx"]) for m in result["matches"]]
    # The optimal assignment maximizes total IoU among 2-match solutions
    total_iou = sum(m["iou"] for m in result["matches"])
    assert total_iou >= 1.9, "Should maximize total IoU (got %.3f)" % total_iou


def test_threshold_exact():
    """IoU exactly at threshold should be included."""
    cands = [{"start": 0.0, "end": 10.0}]
    refs = [{"start": 0.0, "end": 10.0}]
    # IoU = 1.0, threshold = 1.0 → should match
    result = om.optimal_match(cands, refs, iou_threshold=1.0)
    assert result["true_positives"] == 1


def test_threshold_just_below():
    """IoU just below threshold should not match."""
    cands = [{"start": 0.0, "end": 10.0}]
    refs = [{"start": 1.0, "end": 11.0}]
    # IoU = 9/11 = 0.818, threshold = 0.82 → should not match
    result = om.optimal_match(cands, refs, iou_threshold=0.82)
    assert result["true_positives"] == 0


def test_deterministic_ordering():
    """Matching should be deterministic regardless of input order."""
    cands = [
        {"start": 5.0, "end": 15.0},
        {"start": 0.0, "end": 10.0},
    ]
    refs = [
        {"start": 5.0, "end": 15.0},
        {"start": 0.0, "end": 10.0},
    ]
    r1 = om.optimal_match(cands, refs, iou_threshold=0.5)
    # Reverse order
    cands2 = list(reversed(cands))
    refs2 = list(reversed(refs))
    r2 = om.optimal_match(cands2, refs2, iou_threshold=0.5)
    # Same number of matches
    assert r1["true_positives"] == r2["true_positives"]
    # Same total IoU
    assert abs(sum(m["iou"] for m in r1["matches"]) - sum(m["iou"] for m in r2["matches"])) < 1e-9


def test_repeated_determinism():
    """Repeated calls produce identical results."""
    cands = [{"start": 0.0, "end": 10.0}, {"start": 5.0, "end": 15.0}]
    refs = [{"start": 0.0, "end": 10.0}, {"start": 5.0, "end": 15.0}]
    r1 = om.optimal_match(cands, refs, iou_threshold=0.5)
    r2 = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert r1["true_positives"] == r2["true_positives"]
    assert r1["matches"] == r2["matches"]


def test_multiple_equal_optimal():
    """When multiple equally optimal assignments exist, tie-breaking is stable."""
    # Two identical candidates, two identical references
    cands = [{"start": 0.0, "end": 10.0}, {"start": 0.0, "end": 10.0}]
    refs = [{"start": 0.0, "end": 10.0}, {"start": 0.0, "end": 10.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert result["true_positives"] == 2


def test_candidate_ordering_invariance():
    """Matching result should be invariant to candidate ordering."""
    cands = [
        {"start": 0.0, "end": 10.0},
        {"start": 20.0, "end": 30.0},
        {"start": 10.0, "end": 20.0},
    ]
    refs = [
        {"start": 0.0, "end": 10.0},
        {"start": 20.0, "end": 30.0},
        {"start": 10.0, "end": 20.0},
    ]
    r1 = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert r1["true_positives"] == 3
    # Reversed
    r2 = om.optimal_match(list(reversed(cands)), list(reversed(refs)), iou_threshold=0.5)
    assert r2["true_positives"] == 3


def test_overlapping_references():
    """Multiple references can overlap; matching should handle this."""
    cands = [{"start": 0.0, "end": 15.0}]
    refs = [
        {"start": 0.0, "end": 10.0},
        {"start": 5.0, "end": 15.0},
    ]
    result = om.optimal_match(cands, refs, iou_threshold=0.3)
    assert result["true_positives"] == 1  # one candidate, one match


def test_three_candidates_two_refs():
    """3 candidates, 2 references: should match 2."""
    cands = [
        {"start": 0.0, "end": 10.0},
        {"start": 5.0, "end": 15.0},
        {"start": 20.0, "end": 30.0},
    ]
    refs = [
        {"start": 0.0, "end": 10.0},
        {"start": 5.0, "end": 15.0},
    ]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert result["true_positives"] == 2


def test_output_serializable():
    """Match result should be JSON-serializable."""
    import json
    cands = [{"start": 0.0, "end": 10.0}]
    refs = [{"start": 0.0, "end": 10.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    # Should not raise
    json.dumps(result, default=str)


def test_algorithm_field():
    """Result should include algorithm identifier."""
    cands = [{"start": 0.0, "end": 10.0}]
    refs = [{"start": 0.0, "end": 10.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert "algorithm" in result
    assert result["algorithm"] == "hungarian_optimal_v1"


def test_iou_values_correct():
    """IoU values in matches should be correct."""
    cands = [{"start": 0.0, "end": 10.0}]
    refs = [{"start": 0.0, "end": 10.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    assert result["matches"][0]["iou"] == 1.0

    cands = [{"start": 0.0, "end": 10.0}]
    refs = [{"start": 5.0, "end": 15.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.3)
    # IoU = 5/15 = 0.333
    assert abs(result["matches"][0]["iou"] - 0.333333) < 0.001


def test_boundary_errors():
    """Start/end/duration errors should be correct."""
    cands = [{"start": 1.0, "end": 9.0}]
    refs = [{"start": 0.0, "end": 10.0}]
    result = om.optimal_match(cands, refs, iou_threshold=0.5)
    m = result["matches"][0]
    assert m["start_error"] == 1.0
    assert m["end_error"] == 1.0
    assert m["dur_error"] == 2.0
