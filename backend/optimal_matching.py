"""
optimal_matching.py — Optimal bipartite matching for candidate-reference evaluation.

Implements the Hungarian algorithm for maximum-weight bipartite matching
with lexicographic objective:
  1. Maximize cardinality (number of valid matches)
  2. Among max cardinality, maximize total IoU
  3. Deterministic tie-breaking by candidate/ref indices

No external dependencies (no scipy). Pure Python.

Used by benchmark_dataset.py for candidate-to-reference matching.
"""
from __future__ import annotations

import math
from typing import Any


def hungarian_match(
    weights: list[list[float]],
    threshold: float = 0.0,
    big_m: float = 1e6,
) -> list[tuple[int, int]]:
    """Find optimal one-to-one matching using Hungarian algorithm.

    Args:
        weights: n x m matrix of edge weights (IoU values).
        threshold: minimum weight for a valid edge.
        big_m: large constant for lexicographic cardinality priority.

    Returns:
        List of (row, col) matched pairs.
    """
    n = len(weights)
    if n == 0:
        return []
    m = len(weights[0]) if n > 0 else 0
    if m == 0:
        return []

    # Build cost matrix with (n+m) x (n+m) dimensions.
    # Rows 0..n-1: real candidates
    # Rows n..n+m-1: dummy (for unmatched references)
    # Cols 0..m-1: real references
    # Cols m..m+n-1: dummy (for unmatched candidates)
    #
    # Cost values (minimization):
    #   valid real-real:     -(big_m + iou)  [attractive, prioritizes cardinality then IoU]
    #   invalid real-real:   big_m * 10      [prohibited]
    #   real candidate - dummy col:  0       [unmatched candidate, neutral]
    #   dummy row - real reference:  0       [unmatched reference, neutral]
    #   dummy-dummy:                 0       [neutral]
    #
    # This ensures every real candidate can be either matched to a valid
    # reference (negative cost) or left unmatched (zero cost via dummy column).
    # Invalid edges are never preferred over unmatched (big_m*10 >> 0).
    size = n + m
    cost = [[0.0] * size for _ in range(size)]

    for i in range(n):
        for j in range(m):
            iou = weights[i][j]
            if iou >= threshold - 1e-9:
                cost[i][j] = -(big_m + iou)
            else:
                cost[i][j] = big_m * 10  # prohibit

    # Dummy rows (n..n+m-1) and dummy cols (m..m+n-1) already 0.0
    # Real candidate to dummy col: 0.0 (already set)
    # Dummy row to real ref: 0.0 (already set)

    # For dummy rows (i >= n) and dummy cols (j >= m): cost = 0 (free assignment)
    # Hungarian algorithm (minimization)
    assignment = _hungarian(cost, size)
    # Extract valid matches: only real candidate rows (0..n-1) assigned to real ref cols (0..m-1)
    result = []
    for i in range(n):
        j = assignment[i]
        if j < m and weights[i][j] >= threshold - 1e-9:
            result.append((i, j))
    return result


def _hungarian(cost: list[list[float]], n: int) -> list[int]:
    """Hungarian algorithm for assignment problem (minimization).

    Returns assignment[i] = column assigned to row i.
    Standard O(n^3) implementation.
    """
    # 1-indexed arrays for convenience
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = row assigned to column j
    way = [0] * (n + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    # Build assignment (size = n, passed as parameter)
    assignment = [0] * n
    for j in range(1, n + 1):
        if p[j] != 0:
            assignment[p[j] - 1] = j - 1
    return assignment


def optimal_match(
    candidates: list[dict],
    references: list[Any],
    iou_threshold: float = 0.5,
    get_ref_start: Any = None,
    get_ref_end: Any = None,
) -> dict:
    """Perform optimal one-to-one matching between candidates and references.

    Args:
        candidates: list of dicts with 'start' and 'end' keys.
        references: list of reference objects (dicts or dataclass instances).
        iou_threshold: minimum IoU for a valid match.
        get_ref_start: function to get start from reference (default: r.start or r['start']).
        get_ref_end: function to get end from reference (default: r.end or r['end']).

    Returns:
        dict with matches, unmatched_candidates, unmatched_references,
        true_positives, false_positives, false_negatives, threshold, algorithm.
    """
    if get_ref_start is None:
        def get_ref_start(r):
            if isinstance(r, dict):
                return float(r.get("start", 0.0))
            return float(getattr(r, "start", 0.0))
    if get_ref_end is None:
        def get_ref_end(r):
            if isinstance(r, dict):
                return float(r.get("end", 0.0))
            return float(getattr(r, "end", 0.0))

    n = len(candidates)
    m = len(references)

    if n == 0 or m == 0:
        return {
            "threshold": iou_threshold,
            "matches": [],
            "unmatched_candidates": list(candidates),
            "unmatched_references": list(references),
            "true_positives": 0,
            "false_positives": n,
            "false_negatives": m,
            "algorithm": "hungarian_optimal_v1",
        }

    # Build IoU weight matrix
    weights = [[0.0] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            s1, e1 = float(candidates[i]["start"]), float(candidates[i]["end"])
            s2, e2 = get_ref_start(references[j]), get_ref_end(references[j])
            inter = max(0.0, min(e1, e2) - max(s1, s2))
            union = (e1 - s1) + (e2 - s2) - inter
            iou = inter / union if union > 0 else 0.0
            weights[i][j] = iou

    # Run Hungarian
    matched_pairs = hungarian_match(weights, threshold=iou_threshold)

    # Build result
    matches = []
    used_cands = set()
    used_refs = set()
    for ci, ri in matched_pairs:
        c = candidates[ci]
        r = references[ri]
        iou = weights[ci][ri]
        matches.append({
            "candidate_idx": ci,
            "reference_idx": ri,
            "iou": round(iou, 6),
            "candidate": c,
            "reference": r,
            "start_error": round(abs(float(c["start"]) - get_ref_start(r)), 6),
            "end_error": round(abs(float(c["end"]) - get_ref_end(r)), 6),
            "dur_error": round(abs((float(c["end"]) - float(c["start"])) - (get_ref_end(r) - get_ref_start(r))), 6),
        })
        used_cands.add(ci)
        used_refs.add(ri)

    unmatched_cands = [c for i, c in enumerate(candidates) if i not in used_cands]
    unmatched_refs = [r for i, r in enumerate(references) if i not in used_refs]

    return {
        "threshold": iou_threshold,
        "matches": matches,
        "unmatched_candidates": unmatched_cands,
        "unmatched_references": unmatched_refs,
        "true_positives": len(matches),
        "false_positives": len(unmatched_cands),
        "false_negatives": len(unmatched_refs),
        "algorithm": "hungarian_optimal_v1",
    }
