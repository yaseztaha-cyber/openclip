"""
failure_analysis.py — Editorial failure analysis for benchmark results.

Analyzes false positives and false negatives from candidate-reference matching.
Identifies patterns in missed and incorrectly selected moments.

All analysis is local. No network. No LLM.
"""
from __future__ import annotations

import os
import json
import logging
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class FailureRecord:
    """A single failure analysis record."""
    failure_type: str = ""  # "false_positive" or "false_negative"
    video_id: str = ""
    candidate_id: str = ""
    reference_id: str = ""
    candidate_start: float = 0.0
    candidate_end: float = 0.0
    reference_start: float = 0.0
    reference_end: float = 0.0
    iou: float = 0.0
    machine_rank: int = 0
    machine_score: float = 0.0
    human_importance: int = 0
    human_labels: list = field(default_factory=list)
    rejection_reasons: list = field(default_factory=list)
    transcript_context: str = ""
    audio_features: dict = field(default_factory=dict)
    visual_features: dict = field(default_factory=dict)
    likely_cause: str = ""
    confidence: str = "uncertain"  # "certain", "likely", "uncertain"
    notes: str = ""


@dataclass
class FailureAnalysisReport:
    """Complete failure analysis for a benchmark run."""
    video_id: str = ""
    dataset_version: str = ""
    experiment_id: str = ""
    total_candidates: int = 0
    total_references: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    false_positive_records: list = field(default_factory=list)
    false_negative_records: list = field(default_factory=list)
    failure_patterns: dict = field(default_factory=dict)
    recommendations: list = field(default_factory=list)


def analyze_false_positives(
    candidates: list[dict],
    references: list[Any],
    match_result: dict,
    transcript: list[dict] | None = None,
    audio_sigs: dict | None = None,
    visual_sigs: dict | None = None,
) -> list[FailureRecord]:
    """Analyze false positive candidates (machine candidates that don't match references)."""
    records = []
    unmatched = match_result.get("unmatched_candidates", [])

    for c in unmatched:
        idx = candidates.index(c) if c in candidates else -1
        record = FailureRecord(
            failure_type="false_positive",
            candidate_id="cand_%03d" % idx if idx >= 0 else "unknown",
            candidate_start=c.get("start", 0.0),
            candidate_end=c.get("end", 0.0),
            machine_rank=idx + 1,
            machine_score=float(c.get("viral_score", c.get("editorial_priority_score", 0))),
        )

        # Transcript context
        if transcript:
            overlapping = [s for s in transcript if float(s.get("end", 0)) > record.candidate_start and float(s.get("start", 0)) < record.candidate_end]
            record.transcript_context = " ".join(s.get("text", "") for s in overlapping)

        # Audio features
        if audio_sigs:
            start_sec = max(0, int(record.candidate_start))
            end_sec = min(len(audio_sigs.get("burst", [])), int(record.candidate_end))
            burst = audio_sigs.get("burst", [])[start_sec:end_sec]
            loudness = audio_sigs.get("loudness", [])[start_sec:end_sec]
            record.audio_features = {
                "mean_burst": sum(burst) / len(burst) if burst else 0,
                "peak_burst": max(burst) if burst else 0,
                "mean_loudness": sum(loudness) / len(loudness) if loudness else 0,
            }

        # Visual features
        if visual_sigs:
            start_sec = max(0, int(record.candidate_start))
            end_sec = min(len(visual_sigs.get("motion", [])), int(record.candidate_end))
            motion = visual_sigs.get("motion", [])[start_sec:end_sec]
            scene = visual_sigs.get("scene_cut", [])[start_sec:end_sec]
            record.visual_features = {
                "mean_motion": sum(motion) / len(motion) if motion else 0,
                "scene_transitions": sum(1 for v in scene if v > 0.5),
            }

        # Determine likely cause
        warnings = c.get("warnings", [])
        if "starts_mid_sentence" in warnings or "start_mid_sentence" in warnings:
            record.likely_cause = "starts_mid_sentence"
            record.confidence = "likely"
        elif "ends_mid_sentence" in warnings or "end_mid_sentence" in warnings:
            record.likely_cause = "ends_mid_sentence"
            record.confidence = "likely"
        elif record.audio_features.get("mean_burst", 0) > 0.7:
            record.likely_cause = "high_audio_burst_misleading"
            record.confidence = "likely"
        elif record.visual_features.get("scene_transitions", 0) > 2:
            record.likely_cause = "scene_transition_false_positive"
            record.confidence = "likely"
        else:
            record.likely_cause = "unknown"
            record.confidence = "uncertain"

        records.append(record)

    return records


def analyze_false_negatives(
    candidates: list[dict],
    references: list[Any],
    match_result: dict,
    transcript: list[dict] | None = None,
) -> list[FailureRecord]:
    """Analyze false negative references (human moments not matched by any candidate)."""
    records = []
    unmatched = match_result.get("unmatched_references", [])

    for r in unmatched:
        start = r.start if hasattr(r, "start") else r.get("start", 0.0)
        end = r.end if hasattr(r, "end") else r.get("end", 0.0)
        title = r.title if hasattr(r, "title") else r.get("title", "")
        importance = r.importance if hasattr(r, "importance") else r.get("importance", 0)
        categories = r.categories if hasattr(r, "categories") else r.get("categories", [])

        record = FailureRecord(
            failure_type="false_negative",
            reference_id=r.moment_id if hasattr(r, "moment_id") else r.get("moment_id", ""),
            reference_start=start,
            reference_end=end,
            human_importance=importance,
            human_labels=categories,
        )

        # Transcript context
        if transcript:
            overlapping = [s for s in transcript if float(s.get("end", 0)) > start and float(s.get("start", 0)) < end]
            record.transcript_context = " ".join(s.get("text", "") for s in overlapping)

        # Determine likely cause
        ref_duration = end - start
        if ref_duration < 10.0:
            record.likely_cause = "reference_too_short"
            record.confidence = "likely"
        elif ref_duration > 120.0:
            record.likely_cause = "reference_too_long"
            record.confidence = "likely"
        else:
            # Check if any candidate partially overlaps
            partial_overlap = False
            for c in candidates:
                iou = _iou(c["start"], c["end"], start, end)
                if 0 < iou < 0.3:
                    partial_overlap = True
                    break
            if partial_overlap:
                record.likely_cause = "partial_overlap_below_threshold"
                record.confidence = "likely"
            else:
                record.likely_cause = "no_candidate_generated"
                record.confidence = "uncertain"

        records.append(record)

    return records


def _iou(s1: float, e1: float, s2: float, e2: float) -> float:
    inter = max(0.0, min(e1, e2) - max(s1, s2))
    union = (e1 - s1) + (e2 - s2) - inter
    if union <= 0:
        return 0.0
    return inter / union


def analyze_failure_patterns(records: list[FailureRecord]) -> dict:
    """Analyze patterns across failure records."""
    patterns = {
        "false_positive_causes": {},
        "false_negative_causes": {},
        "total_fp": 0,
        "total_fn": 0,
    }

    for r in records:
        if r.failure_type == "false_positive":
            patterns["total_fp"] += 1
            cause = r.likely_cause
            patterns["false_positive_causes"][cause] = patterns["false_positive_causes"].get(cause, 0) + 1
        elif r.failure_type == "false_negative":
            patterns["total_fn"] += 1
            cause = r.likely_cause
            patterns["false_negative_causes"][cause] = patterns["false_negative_causes"].get(cause, 0) + 1

    return patterns


def generate_failure_report(
    video_id: str,
    candidates: list[dict],
    references: list[Any],
    match_result: dict,
    transcript: list[dict] | None = None,
    audio_sigs: dict | None = None,
    visual_sigs: dict | None = None,
    dataset_version: str = "",
    experiment_id: str = "",
) -> FailureAnalysisReport:
    """Generate a complete failure analysis report."""
    report = FailureAnalysisReport()
    report.video_id = video_id
    report.dataset_version = dataset_version
    report.experiment_id = experiment_id
    report.total_candidates = len(candidates)
    report.total_references = len(references)
    report.true_positives = match_result.get("true_positives", 0)
    report.false_positives = match_result.get("false_positives", 0)
    report.false_negatives = match_result.get("false_negatives", 0)

    fp_records = analyze_false_positives(candidates, references, match_result, transcript, audio_sigs, visual_sigs)
    fn_records = analyze_false_negatives(candidates, references, match_result, transcript)

    report.false_positive_records = [asdict(r) for r in fp_records]
    report.false_negative_records = [asdict(r) for r in fn_records]
    report.failure_patterns = analyze_failure_patterns(fp_records + fn_records)

    # Generate recommendations based on patterns
    recommendations = []
    fp_causes = report.failure_patterns.get("false_positive_causes", {})
    if fp_causes.get("starts_mid_sentence", 0) > 0:
        recommendations.append("Improve sentence boundary detection at clip starts")
    if fp_causes.get("high_audio_burst_misleading", 0) > 0:
        recommendations.append("Reduce over-reliance on audio burst for ranking")
    if fp_causes.get("scene_transition_false_positive", 0) > 0:
        recommendations.append("Distinguish scene transitions from editorial interest")

    fn_causes = report.failure_patterns.get("false_negative_causes", {})
    if fn_causes.get("no_candidate_generated", 0) > 0:
        recommendations.append("Expand candidate generation to cover more of the video")
    if fn_causes.get("partial_overlap_below_threshold", 0) > 0:
        recommendations.append("Consider lowering IoU threshold or improving boundary alignment")

    report.recommendations = recommendations
    return report


def save_failure_report(report: FailureAnalysisReport, path: str) -> None:
    """Save failure analysis report as JSON."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, default=str)
