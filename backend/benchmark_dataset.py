"""
benchmark_dataset.py — Versioned editorial benchmark dataset.

Manages benchmark videos, human annotations, and dataset versioning.
All data is local. No network. No paid APIs.

Dataset structure:
  data/evaluation/benchmarks/
    <dataset_version>/
      dataset.json          — dataset metadata + video list
      annotations/
        <video_id>/
          <annotator_id>.json  — individual annotations
      reviews/
        <video_id>/
          <reviewer_id>.json   — candidate reviews
      results/
        <run_id>/             — benchmark results
"""
from __future__ import annotations

import json
import os
import hashlib
import time
import logging
import math
import threading
import tempfile
from typing import Any, Optional, Callable
from dataclasses import dataclass, field, asdict

import sys as _sys
import os as _os

# Cross-process file locking
if _sys.platform == "win32":
    import msvcrt as _msvcrt
    def _acquire_file_lock(lock_path: str, timeout: float = 5.0) -> int:
        """Acquire a cross-process file lock on Windows using msvcrt."""
        import time as _time
        fd = _os.open(lock_path, _os.O_RDWR | _os.O_CREAT, 0o644)
        deadline = _time.monotonic() + timeout
        while True:
            try:
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                return fd
            except (OSError, IOError):
                if _time.monotonic() > deadline:
                    _os.close(fd)
                    raise TimeoutError("Could not acquire lock: %s" % lock_path)
                _time.sleep(0.05)
    def _release_file_lock(fd: int, lock_path: str) -> None:
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except (OSError, IOError):
            pass
        _os.close(fd)
else:
    import fcntl as _fcntl
    def _acquire_file_lock(lock_path: str, timeout: float = 5.0) -> int:
        import time as _time
        fd = _os.open(lock_path, _os.O_RDWR | _os.O_CREAT, 0o644)
        deadline = _time.monotonic() + timeout
        while True:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                return fd
            except (OSError, IOError):
                if _time.monotonic() > deadline:
                    _os.close(fd)
                    raise TimeoutError("Could not acquire lock: %s" % lock_path)
                _time.sleep(0.05)
    def _release_file_lock(fd: int, lock_path: str) -> None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        _os.close(fd)


def _get_lock_path(path: str) -> str:
    """Get the lock file path for a given data file."""
    return path + ".lock"


_path_locks = {}
_locks_guard = __import__('threading').Lock()

def _get_path_lock(path):
    with _locks_guard:
        if path not in _path_locks:
            _path_locks[path] = __import__('threading').RLock()
        return _path_locks[path]

def _atomic_write_json(path: str, data: Any) -> None:
    """Atomically write JSON data to a file.

    Uses temp file + fsync + os.replace for crash safety.
    Thread-safe via per-path RLock.
    Cross-process safe via file locking.
    """
    lock = _get_path_lock(path)
    lock_path = _get_lock_path(path)
    with lock:
        fd = _acquire_file_lock(lock_path)
        try:
            dir_name = _os.path.dirname(_os.path.abspath(path))
            _os.makedirs(dir_name, exist_ok=True)

            # Serialize first
            try:
                content = json.dumps(data, indent=2, default=str)
            except (TypeError, ValueError) as e:
                raise ValueError("Failed to serialize data: %s" % e)

            # Create temp file
            tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with _os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    _os.fsync(f.fileno())
                _os.replace(tmp_path, path)
            except Exception:
                try:
                    if _os.path.exists(tmp_path):
                        _os.remove(tmp_path)
                except OSError:
                    pass
                raise
        finally:
            _release_file_lock(fd, lock_path)


def _read_json_safe(path: str) -> Any:
    """Read JSON from file, returning None if file doesn't exist or is corrupted."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

logger = logging.getLogger(__name__)

DATASET_VERSION = "phase4-v1"
ANNOTATION_GUIDELINE_VERSION = "v1.0"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VideoMetadata:
    """Metadata for a benchmark video."""
    video_id: str = ""
    source_hash: str = ""  # SHA-256 of source file
    duration: float = 0.0
    resolution: str = ""  # "WxH"
    fps: float = 0.0
    has_audio: bool = True
    has_transcript: bool = False
    permission_status: str = "pending"  # pending, authorized, denied
    permission_evidence: str = ""  # reference to permission documentation
    media_type: str = "real"  # real, synthetic
    added_at: str = ""
    dataset_version: str = DATASET_VERSION


@dataclass
class InterestingMoment:
    """A human-annotated interesting moment in a video."""
    moment_id: str = ""
    video_id: str = ""
    start: float = 0.0
    end: float = 0.0
    title: str = ""
    description: str = ""
    why_valuable: str = ""
    categories: list = field(default_factory=list)
    importance: int = 3  # 1-5
    confidence: int = 3  # 1-5
    annotator_id: str = ""
    annotation_timestamp: str = ""
    guideline_version: str = ANNOTATION_GUIDELINE_VERSION
    notes: str = ""


@dataclass
class CandidateReview:
    """Human review of a generated candidate."""
    review_id: str = ""
    candidate_id: str = ""
    video_id: str = ""
    reviewer_id: str = ""
    review_timestamp: str = ""
    opening_hook: int = 0  # 1-5
    context_completeness: int = 0
    interesting_moment: int = 0
    ending_payoff: int = 0
    audio_clarity: int = 0
    visual_quality: int = 0
    overall_usefulness: int = 0
    label: str = ""  # Excellent/Good/Acceptable/Weak/Reject
    rejection_reasons: list = field(default_factory=list)
    notes: str = ""


@dataclass
class DatasetManifest:
    """Manifest for a benchmark dataset version."""
    dataset_version: str = DATASET_VERSION
    created_at: str = ""
    annotation_guideline_version: str = ANNOTATION_GUIDELINE_VERSION
    videos: list = field(default_factory=list)  # list of VideoMetadata dicts
    annotators: list = field(default_factory=list)
    total_annotations: int = 0
    total_reviews: int = 0


# ---------------------------------------------------------------------------
# Editorial categories
# ---------------------------------------------------------------------------

EDITORIAL_CATEGORIES = [
    "strong_opening",
    "surprising_information",
    "emotional_reaction",
    "important_explanation",
    "funny_moment",
    "demonstration",
    "story_payoff",
    "strong_opinion",
    "transformation",
    "useful_information",
    "other",
]

REJECTION_REASONS = [
    "starts_mid_sentence",
    "ends_mid_sentence",
    "missing_context",
    "excessive_silence",
    "uninteresting_moment",
    "poor_audio",
    "poor_framing",
    "duplicate_moment",
    "incorrect_duration",
    "other",
]

LABELS = ["Excellent", "Good", "Acceptable", "Weak", "Reject"]


# ---------------------------------------------------------------------------
# Dataset management
# ---------------------------------------------------------------------------

def get_dataset_dir(base_dir: str, version: str = DATASET_VERSION) -> str:
    """Get the directory for a dataset version."""
    return os.path.join(base_dir, "evaluation", "benchmarks", version)


def init_dataset(base_dir: str, version: str = DATASET_VERSION) -> str:
    """Initialize a dataset directory structure."""
    ddir = get_dataset_dir(base_dir, version)
    os.makedirs(os.path.join(ddir, "annotations"), exist_ok=True)
    os.makedirs(os.path.join(ddir, "reviews"), exist_ok=True)
    os.makedirs(os.path.join(ddir, "results"), exist_ok=True)

    manifest_path = os.path.join(ddir, "dataset.json")
    if not os.path.exists(manifest_path):
        manifest = DatasetManifest(
            dataset_version=version,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        _atomic_write_json(manifest_path, asdict(manifest))
    return ddir


def add_video(base_dir: str, meta: VideoMetadata, version: str = DATASET_VERSION) -> None:
    """Add a video to the dataset."""
    ddir = get_dataset_dir(base_dir, version)
    os.makedirs(ddir, exist_ok=True)
    manifest_path = os.path.join(ddir, "dataset.json")
    manifest = _load_manifest(manifest_path)
    # Check for duplicate video_id
    for v in manifest.get("videos", []):
        if v.get("video_id") == meta.video_id:
            logger.warning("Video %s already in dataset, updating", meta.video_id)
            manifest["videos"] = [x for x in manifest["videos"] if x.get("video_id") != meta.video_id]
            break
    manifest["videos"].append(asdict(meta))
    _save_manifest(manifest_path, manifest)


def _load_manifest(path: str) -> dict:
    if not os.path.exists(path):
        return asdict(DatasetManifest())
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_manifest(path: str, manifest: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _atomic_write_json(path, manifest)


def get_videos(base_dir: str, version: str = DATASET_VERSION) -> list[dict]:
    """Get all videos in the dataset."""
    manifest = _load_manifest(os.path.join(get_dataset_dir(base_dir, version), "dataset.json"))
    return manifest.get("videos", [])


def get_authorized_videos(base_dir: str, version: str = DATASET_VERSION) -> list[dict]:
    """Get only authorized videos."""
    return [v for v in get_videos(base_dir, version) if v.get("permission_status") == "authorized"]


# ---------------------------------------------------------------------------
# Annotation management
# ---------------------------------------------------------------------------

def _annotation_path(base_dir: str, video_id: str, annotator_id: str, version: str = DATASET_VERSION) -> str:
    return os.path.join(get_dataset_dir(base_dir, version), "annotations", video_id, "%s.json" % annotator_id)


def save_annotation(base_dir: str, moment: InterestingMoment, version: str = DATASET_VERSION) -> None:
    """Save a human-annotated interesting moment."""
    path = _annotation_path(base_dir, moment.video_id, moment.annotator_id, version)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Load existing annotations for this annotator
    existing = _read_json_safe(path)
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        existing = [existing]

    # Validate moment
    _validate_moment(moment)

    # Append (preserve original annotations)
    existing.append(asdict(moment))
    _atomic_write_json(path, existing)


def load_annotations(base_dir: str, video_id: str, annotator_id: str | None = None, version: str = DATASET_VERSION) -> list[InterestingMoment]:
    """Load annotations for a video, optionally filtered by annotator."""
    annotations_dir = os.path.join(get_dataset_dir(base_dir, version), "annotations", video_id)
    if not os.path.exists(annotations_dir):
        return []

    result = []
    if annotator_id:
        path = os.path.join(annotations_dir, "%s.json" % annotator_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = [data]
            result.extend(InterestingMoment(**d) for d in data)
    else:
        for fname in os.listdir(annotations_dir):
            if fname.endswith(".json"):
                path = os.path.join(annotations_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    data = [data]
                result.extend(InterestingMoment(**d) for d in data)
    return result


def get_annotators(base_dir: str, video_id: str, version: str = DATASET_VERSION) -> list[str]:
    """Get list of annotators for a video."""
    annotations_dir = os.path.join(get_dataset_dir(base_dir, version), "annotations", video_id)
    if not os.path.exists(annotations_dir):
        return []
    return [f.replace(".json", "") for f in os.listdir(annotations_dir) if f.endswith(".json")]


def _validate_moment(moment: InterestingMoment) -> None:
    """Validate an interesting moment annotation."""
    if moment.end <= moment.start:
        raise ValueError("End must be after start: %s" % moment.moment_id)
    if moment.start < 0:
        raise ValueError("Start must be non-negative: %s" % moment.moment_id)
    if moment.importance < 1 or moment.importance > 5:
        raise ValueError("Importance must be 1-5: %s" % moment.moment_id)
    if moment.confidence < 1 or moment.confidence > 5:
        raise ValueError("Confidence must be 1-5: %s" % moment.moment_id)
    for cat in moment.categories:
        if cat not in EDITORIAL_CATEGORIES:
            raise ValueError("Unknown category %s: %s" % (cat, moment.moment_id))


# ---------------------------------------------------------------------------
# Candidate review management
# ---------------------------------------------------------------------------

def _review_path(base_dir: str, video_id: str, reviewer_id: str, version: str = DATASET_VERSION) -> str:
    return os.path.join(get_dataset_dir(base_dir, version), "reviews", video_id, "%s.json" % reviewer_id)


def save_candidate_review(base_dir: str, review: CandidateReview, version: str = DATASET_VERSION) -> None:
    """Save a candidate review annotation."""
    path = _review_path(base_dir, review.video_id, review.reviewer_id, version)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    existing = _read_json_safe(path)
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        existing = [existing]

    # Validate rating fields
    for field_name in ["opening_hook", "context_completeness", "interesting_moment",
                       "ending_payoff", "audio_clarity", "visual_quality", "overall_usefulness"]:
        val = getattr(review, field_name, 0)
        if val != 0 and (val < 1 or val > 5):
            raise ValueError("%s must be 0 or 1-5: %s" % (field_name, review.review_id))

    if review.label and review.label not in LABELS:
        raise ValueError("Unknown label: %s" % review.label)

    for reason in review.rejection_reasons:
        if reason not in REJECTION_REASONS:
            raise ValueError("Unknown rejection reason: %s" % reason)

    existing.append(asdict(review))
    _atomic_write_json(path, existing)


def load_candidate_reviews(base_dir: str, video_id: str, reviewer_id: str | None = None, version: str = DATASET_VERSION) -> list[CandidateReview]:
    """Load candidate reviews for a video."""
    reviews_dir = os.path.join(get_dataset_dir(base_dir, version), "reviews", video_id)
    if not os.path.exists(reviews_dir):
        return []

    result = []
    if reviewer_id:
        path = os.path.join(reviews_dir, "%s.json" % reviewer_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = [data]
            result.extend(CandidateReview(**d) for d in data)
    else:
        for fname in os.listdir(reviews_dir):
            if fname.endswith(".json"):
                path = os.path.join(reviews_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    data = [data]
                result.extend(CandidateReview(**d) for d in data)
    return result


def get_reviewers(base_dir: str, video_id: str, version: str = DATASET_VERSION) -> list[str]:
    """Get list of reviewers for a video."""
    reviews_dir = os.path.join(get_dataset_dir(base_dir, version), "reviews", video_id)
    if not os.path.exists(reviews_dir):
        return []
    return [f.replace(".json", "") for f in os.listdir(reviews_dir) if f.endswith(".json")]


# ---------------------------------------------------------------------------
# Reviewer agreement
# ---------------------------------------------------------------------------

def compute_boundary_agreement(annotations_list: list[list[InterestingMoment]]) -> dict:
    """Compute boundary agreement across multiple annotators.

    annotations_list: list of annotation lists, one per annotator.
    """
    if len(annotations_list) < 2:
        return {"status": "insufficient_annotators", "annotator_count": len(annotations_list)}

    # Flatten all moments
    all_moments = []
    for annotator_idx, moments in enumerate(annotations_list):
        for m in moments:
            all_moments.append((annotator_idx, m))

    # Compute pairwise boundary IoU for matched moments
    ious = []
    start_diffs = []
    end_diffs = []
    dur_diffs = []

    for i in range(len(annotations_list)):
        for j in range(i + 1, len(annotations_list)):
            for m1 in annotations_list[i]:
                for m2 in annotations_list[j]:
                    # Match by temporal overlap
                    iou = _temporal_iou(m1.start, m1.end, m2.start, m2.end)
                    if iou > 0.3:  # potential match
                        ious.append(iou)
                        start_diffs.append(abs(m1.start - m2.start))
                        end_diffs.append(abs(m1.end - m2.end))
                        dur_diffs.append(abs((m1.end - m1.start) - (m2.end - m2.start)))

    if not ious:
        return {
            "status": "no_overlapping_moments",
            "annotator_count": len(annotations_list),
            "matched_pairs": 0,
        }

    return {
        "status": "ok",
        "annotator_count": len(annotations_list),
        "matched_pairs": len(ious),
        "mean_iou": sum(ious) / len(ious),
        "mean_start_diff": sum(start_diffs) / len(start_diffs),
        "mean_end_diff": sum(end_diffs) / len(end_diffs),
        "mean_dur_diff": sum(dur_diffs) / len(dur_diffs),
        "min_iou": min(ious),
        "max_iou": max(ious),
    }


def _temporal_iou(s1: float, e1: float, s2: float, e2: float) -> float:
    inter = max(0.0, min(e1, e2) - max(s1, s2))
    union = (e1 - s1) + (e2 - s2) - inter
    if union <= 0:
        return 0.0
    return inter / union


def compute_importance_agreement(annotations_list: list[list[InterestingMoment]]) -> dict:
    """Compute importance rating agreement across annotators."""
    if len(annotations_list) < 2:
        return {"status": "insufficient_annotators"}

    # Match moments and compare importance
    diffs = []
    for i in range(len(annotations_list)):
        for j in range(i + 1, len(annotations_list)):
            for m1 in annotations_list[i]:
                for m2 in annotations_list[j]:
                    iou = _temporal_iou(m1.start, m1.end, m2.start, m2.end)
                    if iou > 0.3:
                        diffs.append(abs(m1.importance - m2.importance))

    if not diffs:
        return {"status": "no_matched_moments", "matched_pairs": 0}

    return {
        "status": "ok",
        "matched_pairs": len(diffs),
        "mean_importance_diff": sum(diffs) / len(diffs),
        "max_importance_diff": max(diffs),
        "exact_agreement": sum(1 for d in diffs if d == 0) / len(diffs),
    }


def compute_category_agreement(annotations_list: list[list[InterestingMoment]]) -> dict:
    """Compute category agreement across annotators."""
    if len(annotations_list) < 2:
        return {"status": "insufficient_annotators"}

    # Match moments and compare categories
    agreements = []
    for i in range(len(annotations_list)):
        for j in range(i + 1, len(annotations_list)):
            for m1 in annotations_list[i]:
                for m2 in annotations_list[j]:
                    iou = _temporal_iou(m1.start, m1.end, m2.start, m2.end)
                    if iou > 0.3:
                        cats1 = set(m1.categories)
                        cats2 = set(m2.categories)
                        if cats1 or cats2:
                            jaccard = len(cats1 & cats2) / len(cats1 | cats2)
                            agreements.append(jaccard)

    if not agreements:
        return {"status": "no_matched_moments", "matched_pairs": 0}

    return {
        "status": "ok",
        "matched_pairs": len(agreements),
        "mean_category_jaccard": sum(agreements) / len(agreements),
    }


# ---------------------------------------------------------------------------
# Candidate-to-reference matching
# ---------------------------------------------------------------------------

# Import optimal matching
try:
    import optimal_matching as om
    _HAS_OPTIMAL = True
except ImportError:
    _HAS_OPTIMAL = False


def match_candidates_to_references(
    candidates: list[dict],
    references: list[InterestingMoment],
    iou_threshold: float = 0.5,
) -> dict:
    """Match candidates to reference moments using optimal bipartite matching.

    Uses the Hungarian algorithm to maximize cardinality (number of matches)
    then maximize total IoU. This is NOT greedy — it finds the globally
    optimal one-to-one assignment.

    Algorithm: hungarian_optimal_v1 (see optimal_matching.py)
    """
    if _HAS_OPTIMAL:
        return om.optimal_match(
            candidates, references, iou_threshold=iou_threshold,
            get_ref_start=lambda r: r.start,
            get_ref_end=lambda r: r.end,
        )

    # Fallback: greedy (should not happen)
    # ... (keep old implementation as fallback)
    iou_matrix = []
    for c in candidates:
        row = []
        for r in references:
            iou = _temporal_iou(c["start"], c["end"], r.start, r.end)
            row.append(iou)
        iou_matrix.append(row)

    matches = []
    used_cands = set()
    used_refs = set()

    pairs = []
    for ci, c in enumerate(candidates):
        for ri, r in enumerate(references):
            pairs.append((iou_matrix[ci][ri], ci, ri))
    pairs.sort(reverse=True)

    for iou, ci, ri in pairs:
        if iou < iou_threshold - 1e-9:
            break
        if ci in used_cands or ri in used_refs:
            continue
        matches.append({
            "candidate_idx": ci,
            "reference_idx": ri,
            "iou": iou,
            "candidate": candidates[ci],
            "reference": references[ri],
            "start_error": abs(candidates[ci]["start"] - references[ri].start),
            "end_error": abs(candidates[ci]["end"] - references[ri].end),
            "dur_error": abs((candidates[ci]["end"] - candidates[ci]["start"]) - (references[ri].end - references[ri].start)),
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
        "algorithm": "greedy_fallback",
    }


def compute_detection_metrics(match_result: dict) -> dict:
    """Compute precision, recall, F1 from match result."""
    tp = match_result["true_positives"]
    fp = match_result["false_positives"]
    fn = match_result["false_negatives"]

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


def compute_ranking_metrics(
    candidates: list[dict],
    references: list[InterestingMoment],
    k: int = 5,
    iou_threshold: float = 0.5,
) -> dict:
    """Compute ranking quality metrics: Precision@K, Recall@K, NDCG@K."""
    # Determine relevance: candidate is relevant if it matches a reference
    match_result = match_candidates_to_references(candidates, references, iou_threshold)
    matched_indices = {m["candidate_idx"] for m in match_result["matches"]}

    # Relevance labels (binary: 1 if matched, 0 otherwise)
    relevance = [1.0 if i in matched_indices else 0.0 for i in range(len(candidates))]

    # Precision@K
    top_k = min(k, len(candidates))
    prec_at_k = sum(relevance[:top_k]) / top_k if top_k > 0 else 0.0

    # Recall@K
    total_relevant = sum(relevance)
    recall_at_k = sum(relevance[:top_k]) / total_relevant if total_relevant > 0 else 0.0

    # NDCG@K
    ndcg = _ndcg_at_k(relevance, top_k)

    # Average Precision
    ap = _average_precision(relevance)

    return {
        "k": k,
        "iou_threshold": iou_threshold,
        "precision_at_k": round(prec_at_k, 4),
        "recall_at_k": round(recall_at_k, 4),
        "ndcg_at_k": round(ndcg, 4),
        "average_precision": round(ap, 4),
        "total_relevant": int(total_relevant),
        "total_candidates": len(candidates),
    }


def _ndcg_at_k(relevance: list[float], k: int) -> float:
    """Compute NDCG@K with binary relevance."""
    if not relevance or k == 0:
        return 0.0

    # DCG@K
    dcg = 0.0
    for i in range(min(k, len(relevance))):
        dcg += relevance[i] / math.log2(i + 2)  # log2(i+2) because i starts at 0

    # Ideal DCG@K
    ideal = sorted(relevance, reverse=True)
    idcg = 0.0
    for i in range(min(k, len(ideal))):
        idcg += ideal[i] / math.log2(i + 2)

    if idcg == 0:
        return 0.0
    return dcg / idcg


def _average_precision(relevance: list[float]) -> float:
    """Compute Average Precision."""
    if not relevance or sum(relevance) == 0:
        return 0.0
    precisions = []
    tp = 0
    for i, r in enumerate(relevance):
        if r > 0:
            tp += 1
            precisions.append(tp / (i + 1))
    if not precisions:
        return 0.0
    return sum(precisions) / len(precisions)


def compute_boundary_metrics(match_result: dict) -> dict:
    """Compute boundary accuracy metrics from match results."""
    matches = match_result["matches"]
    if not matches:
        return {
            "mean_start_error": 0.0,
            "mean_end_error": 0.0,
            "mean_dur_error": 0.0,
            "max_start_error": 0.0,
            "max_end_error": 0.0,
            "mean_iou": 0.0,
        }

    start_errors = [m["start_error"] for m in matches]
    end_errors = [m["end_error"] for m in matches]
    dur_errors = [m["dur_error"] for m in matches]
    ious = [m["iou"] for m in matches]

    return {
        "mean_start_error": round(sum(start_errors) / len(start_errors), 3),
        "mean_end_error": round(sum(end_errors) / len(end_errors), 3),
        "mean_dur_error": round(sum(dur_errors) / len(dur_errors), 3),
        "max_start_error": round(max(start_errors), 3),
        "max_end_error": round(max(end_errors), 3),
        "mean_iou": round(sum(ious) / len(ious), 4),
        "min_iou": round(min(ious), 4),
        "max_iou": round(max(ious), 4),
    }


def compute_boundary_tolerance(match_result: dict, tolerance: float = 1.0) -> dict:
    """Compute boundary tolerance success rate.

    A match is "tolerant" if both start and end errors are <= tolerance.
    """
    matches = match_result["matches"]
    if not matches:
        return {"tolerance": tolerance, "success_rate": 0.0, "success_count": 0, "total": 0}

    success = sum(1 for m in matches if m["start_error"] <= tolerance and m["end_error"] <= tolerance)
    return {
        "tolerance": tolerance,
        "success_rate": round(success / len(matches), 4),
        "success_count": success,
        "total": len(matches),
    }


# ---------------------------------------------------------------------------
# Ablation support
# ---------------------------------------------------------------------------

def run_ablation(
    transcript: list[dict],
    video_duration: float,
    audio_sigs: dict | None = None,
    visual_sigs: dict | None = None,
    references: list[InterestingMoment] | None = None,
) -> dict:
    """Run ablation study: compare candidate generation with different signal combinations.

    Returns metrics for each configuration:
      - transcript_only: no audio, no visual
      - audio_only: audio signals, no visual
      - visual_only: visual signals, no audio
      - full: both audio and visual
    """
    import deterministic_candidate as dc

    configs = {
        "transcript_only": {"audio_sigs": None, "visual_sigs": None},
        "audio_only": {"audio_sigs": audio_sigs, "visual_sigs": None},
        "visual_only": {"audio_sigs": None, "visual_sigs": visual_sigs},
        "full": {"audio_sigs": audio_sigs, "visual_sigs": visual_sigs},
    }

    results = {}
    for name, cfg in configs.items():
        cands = dc.generate_candidates(
            transcript, video_duration,
            audio_sigs=cfg["audio_sigs"],
            visual_sigs=cfg["visual_sigs"],
        )
        result = {
            "candidate_count": len(cands),
            "diversity": None,
        }
        if references:
            match = match_candidates_to_references(cands, references, 0.5)
            det = compute_detection_metrics(match)
            result["precision"] = det["precision"]
            result["recall"] = det["recall"]
            result["f1"] = det["f1"]
            result["true_positives"] = det["true_positives"]
            result["false_positives"] = det["false_positives"]
            result["false_negatives"] = det["false_negatives"]
        results[name] = result

    return results


# ---------------------------------------------------------------------------
# Editorial ranking optimization
# ---------------------------------------------------------------------------

def analyze_ranking_weaknesses(
    candidates: list[dict],
    references: list[InterestingMoment],
    reviews: list[CandidateReview] | None = None,
) -> dict:
    """Analyze weaknesses in current ranking based on human annotations.

    Identifies:
      - High-ranked candidates that humans rejected
      - Low-ranked candidates that humans rated highly
      - Common rejection reasons among top-ranked candidates
      - Correlation between machine score and human rating
    """
    # Match candidates to references
    match = match_candidates_to_references(candidates, references, 0.5)
    matched_indices = {m["candidate_idx"] for m in match["matches"]}

    # Analyze top-ranked candidates
    ranked = sorted(enumerate(candidates), key=lambda x: x[1].get("editorial_priority_score", x[1].get("viral_score", 0)), reverse=True)
    top_half = ranked[:len(ranked) // 2] if ranked else []
    bottom_half = ranked[len(ranked) // 2:] if ranked else []

    top_rejected = 0
    top_matched = 0
    for idx, c in top_half:
        if idx in matched_indices:
            top_matched += 1
        else:
            top_rejected += 1

    bottom_matched = 0
    for idx, c in bottom_half:
        if idx in matched_indices:
            bottom_matched += 1

    # Reviewer agreement analysis
    review_analysis = {}
    if reviews:
        review_by_candidate = {}
        for r in reviews:
            review_by_candidate.setdefault(r.candidate_id, []).append(r)

        # Correlation between machine viral_score and human overall_usefulness
        correlations = []
        for c in candidates:
            cid = c.get("id", "") or "cand_%03d" % candidates.index(c)
            if cid in review_by_candidate:
                for rev in review_by_candidate[cid]:
                    correlations.append((c.get("editorial_priority_score", c.get("viral_score", 0)), rev.overall_usefulness))

        if correlations:
            # Simple Pearson correlation
            xs = [x[0] for x in correlations]
            ys = [x[1] for x in correlations]
            mean_x = sum(xs) / len(xs)
            mean_y = sum(ys) / len(ys)
            cov = sum((x - mean_x) * (y - mean_y) for x, y in correlations) / len(correlations)
            std_x = (sum((x - mean_x) ** 2 for x in xs) / len(xs)) ** 0.5
            std_y = (sum((y - mean_y) ** 2 for y in ys) / len(ys)) ** 0.5
            corr = cov / (std_x * std_y) if (std_x * std_y) > 0 else 0.0
            review_analysis["machine_human_correlation"] = round(corr, 4)
            review_analysis["review_count"] = len(correlations)

    return {
        "total_candidates": len(candidates),
        "total_references": len(references),
        "matched": len(match["matches"]),
        "top_half_matched": top_matched,
        "top_half_unmatched": top_rejected,
        "bottom_half_matched": bottom_matched,
        "precision": match["true_positives"] / (match["true_positives"] + match["false_positives"]) if (match["true_positives"] + match["false_positives"]) > 0 else 0.0,
        "recall": match["true_positives"] / (match["true_positives"] + match["false_negatives"]) if (match["true_positives"] + match["false_negatives"]) > 0 else 0.0,
        "review_analysis": review_analysis,
    }


# ---------------------------------------------------------------------------
# Evidence-based ranking improvement
# ---------------------------------------------------------------------------

def improve_ranking(
    candidates: list[dict],
    transcript: list[dict],
    audio_sigs: dict | None = None,
    visual_sigs: dict | None = None,
    words: list[dict] | None = None,
) -> list[dict]:
    """Apply evidence-based ranking improvements to candidates.

    Improvements (based on Phase 4 analysis):
    1. Boundary penalty: candidates starting/ending mid-sentence get score penalty
    2. Context bonus: candidates with more complete sentences get bonus
    3. Silence penalty: candidates with high silence ratio get penalty
    4. Diversity bonus: candidates in underrepresented temporal sections get bonus
    5. Loudness normalization: reduce over-reliance on audio burst

    Returns re-ranked candidates with updated viral_score.
    """
    if not candidates:
        return []

    import copy
    improved = [copy.deepcopy(c) for c in candidates]

    # Compute temporal sections for diversity
    if improved:
        min_start = min(c["start"] for c in improved)
        max_end = max(c["end"] for c in improved)
        total_span = max_end - min_start
        section_size = total_span / 5.0 if total_span > 0 else 1.0

    section_counts = [0] * 5
    for c in improved:
        mid = (c["start"] + c["end"]) / 2.0
        sec = min(4, int((mid - min_start) / section_size)) if section_size > 0 else 0
        section_counts[sec] += 1

    for c in improved:
        score = float(c.get("editorial_priority_score", c.get("viral_score", 5)))

        # 1. Boundary penalty
        warnings = c.get("warnings", [])
        if "start_mid_sentence" in warnings or "starts_mid_sentence" in warnings:
            score -= 1.5
        if "end_mid_sentence" in warnings or "ends_mid_sentence" in warnings:
            score -= 1.5
        if "starts_with_filler" in warnings:
            score -= 0.5

        # 2. Context bonus (based on transcript completeness)
        # Check if candidate contains complete sentences
        start = c["start"]
        end = c["end"]
        overlapping = [s for s in transcript if float(s.get("end", 0)) > start and float(s.get("start", 0)) < end]
        complete_count = sum(1 for s in overlapping if str(s.get("text", "")).strip().endswith((".", "?", "!")))
        if overlapping:
            completeness = complete_count / len(overlapping)
            score += completeness * 1.0

        # 3. Silence penalty
        silence = (audio_sigs or {}).get("silence", [])
        if silence:
            start_sec = max(0, int(start))
            end_sec = min(len(silence), int(end))
            window = silence[start_sec:end_sec]
            if window:
                sil_ratio = sum(1 for v in window if v > 0.5) / len(window)
                score -= sil_ratio * 2.0

        # 4. Diversity bonus (underrepresented sections)
        mid = (start + end) / 2.0
        sec = min(4, int((mid - min_start) / section_size)) if section_size > 0 else 0
        if section_counts[sec] <= 1:
            score += 0.5

        # 5. Loudness normalization (reduce over-reliance on burst)
        burst = (audio_sigs or {}).get("burst", [])
        if burst:
            start_sec = max(0, int(start))
            end_sec = min(len(burst), int(end))
            window = burst[start_sec:end_sec]
            if window:
                mean_burst = sum(window) / len(window)
                # Cap burst contribution
                if mean_burst > 0.7:
                    score -= (mean_burst - 0.7) * 2.0

        c["editorial_priority_score"] = max(1, min(10, int(score)))
        c["viral_score"] = c["editorial_priority_score"]  # deprecated alias for backward compat
        c["_improved_score"] = round(score, 2)
        c["_improvement_reasons"] = []

    # Re-sort by improved score
    improved.sort(key=lambda c: (-c.get("_improved_score", 0), c["start"]))

    return improved
