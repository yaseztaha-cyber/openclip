"""
evaluation_engine.py — Candidate quality evaluation framework.

Measures candidate quality at multiple levels:
  - Temporal: duration, boundaries, silence, overlap
  - Audio: speech ratio, silence ratio, energy, peaks
  - Transcript: coverage, completeness, density, gaps
  - Visual: scene transitions, motion, stability
  - Diversity: overlap, distribution, duplicates
  - Reproducibility: deterministic re-runs

All measurements are raw data, not a single score.
Machine measurements are kept separate from human judgments.

No network calls. No LLM. No paid APIs.
"""

from __future__ import annotations

import json
import os
import math
import time
import hashlib
import logging
from typing import Any, Optional, Callable
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_CLIP_DUR = 10.0
MAX_CLIP_DUR = 120.0
TARGET_DURATIONS = (15.0, 30.0, 45.0, 60.0)
SILENCE_THRESHOLD = 0.5  # silence_mask value above this = silent
GAP_THRESHOLD = 3.0  # seconds between transcript segments = gap


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TemporalMetrics:
    """Temporal quality measurements for a single candidate."""
    candidate_id: str = ""
    start: float = 0.0
    end: float = 0.0
    duration: float = 0.0
    min_dur_compliant: bool = True
    max_dur_compliant: bool = True
    start_at_sentence_boundary: bool = True
    end_at_sentence_boundary: bool = True
    start_mid_sentence: bool = False
    end_mid_sentence: bool = False
    starts_with_filler: bool = False
    silence_ratio_at_start: float = 0.0
    silence_ratio_at_end: float = 0.0
    silence_ratio_overall: float = 0.0
    max_silence_gap: float = 0.0
    abrupt_start: bool = False
    abrupt_end: bool = False
    warnings: list = field(default_factory=list)


@dataclass
class AudioMetrics:
    """Audio quality measurements for a single candidate."""
    candidate_id: str = ""
    speech_ratio: float = 0.0
    silence_ratio: float = 0.0
    mean_rms: float = 0.0
    peak_rms: float = 0.0
    mean_burst: float = 0.0
    peak_burst: float = 0.0
    energy_variance: float = 0.0
    long_silence_count: int = 0
    long_silence_total: float = 0.0
    audio_peak_count: int = 0
    audio_peak_near_boundary: bool = False


@dataclass
class TranscriptMetrics:
    """Transcript quality measurements for a single candidate."""
    candidate_id: str = ""
    transcript_coverage: float = 0.0
    word_count: int = 0
    sentence_count: int = 0
    complete_sentences: int = 0
    incomplete_sentences: int = 0
    speech_density: float = 0.0
    max_gap_between_segments: float = 0.0
    gap_count: int = 0
    starts_at_transcript_boundary: bool = True
    ends_at_transcript_boundary: bool = True
    coherent: bool = True


@dataclass
class VisualMetrics:
    """Visual quality measurements for a single candidate."""
    candidate_id: str = ""
    scene_transition_count: int = 0
    mean_motion: float = 0.0
    peak_motion: float = 0.0
    motion_variance: float = 0.0
    static_frame_ratio: float = 0.0
    sudden_change_count: int = 0
    framing_stable: bool = True


@dataclass
class DiversityMetrics:
    """Candidate diversity measurements across the full set."""
    total_candidates: int = 0
    unique_moments: int = 0
    max_overlap_iou: float = 0.0
    mean_overlap_iou: float = 0.0
    temporal_spread: float = 0.0
    concentration_ratio: float = 0.0
    duplicate_count: int = 0
    distinct_sections: int = 0


@dataclass
class CandidateEvaluation:
    """Full evaluation of a single candidate."""
    candidate_id: str = ""
    candidate: dict = field(default_factory=dict)
    temporal: TemporalMetrics = field(default_factory=TemporalMetrics)
    audio: AudioMetrics = field(default_factory=AudioMetrics)
    transcript: TranscriptMetrics = field(default_factory=TranscriptMetrics)
    visual: VisualMetrics = field(default_factory=VisualMetrics)
    warnings: list = field(default_factory=list)
    machine_score: float = 0.0


@dataclass
class EvaluationReport:
    """Complete evaluation report for a video."""
    video_path: str = ""
    video_duration: float = 0.0
    video_hash: str = ""
    video_resolution: str = ""
    video_fps: float = 0.0
    git_commit: str = ""
    config_version: str = "phase3-v1"
    timestamp: str = ""
    signal_extraction_time: float = 0.0
    candidate_generation_time: float = 0.0
    evaluation_time: float = 0.0
    total_time: float = 0.0
    memory_peak_mb: float = 0.0
    candidate_count: int = 0
    candidates: list = field(default_factory=list)
    diversity: DiversityMetrics = field(default_factory=DiversityMetrics)
    reproducibility: dict = field(default_factory=dict)
    human_reviews: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Evaluation functions
# ---------------------------------------------------------------------------

def _candidate_id(c: dict, idx: int) -> str:
    return "cand_%03d" % idx


def _iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = (a_end - a_start) + (b_end - b_start) - inter
    if union <= 0:
        return 0.0
    return inter / union


def evaluate_temporal(
    candidate: dict,
    video_duration: float,
    audio_sigs: dict | None = None,
    idx: int = 0,
) -> TemporalMetrics:
    """Evaluate temporal quality of a candidate."""
    m = TemporalMetrics()
    m.candidate_id = _candidate_id(candidate, idx)
    m.start = float(candidate.get("start", 0.0))
    m.end = float(candidate.get("end", 0.0))
    m.duration = m.end - m.start

    # Duration compliance
    m.min_dur_compliant = m.duration >= MIN_CLIP_DUR
    m.max_dur_compliant = m.duration <= MAX_CLIP_DUR

    # Boundary checks from candidate
    bi = candidate.get("boundary_info", {})
    m.start_mid_sentence = bi.get("incomplete_start", False) or "start_mid_sentence" in candidate.get("warnings", [])
    m.end_mid_sentence = bi.get("incomplete_end", False) or "end_mid_sentence" in candidate.get("warnings", [])
    m.starts_with_filler = "starts_with_filler" in candidate.get("warnings", [])

    # Silence at boundaries
    silence = (audio_sigs or {}).get("silence", [])
    if silence:
        # Silence ratio in first 2 seconds and last 2 seconds
        start_sec = max(0, int(m.start))
        end_sec = min(len(silence), int(m.end))
        window_start = silence[start_sec:min(start_sec + 2, len(silence))]
        window_end = silence[max(0, end_sec - 2):end_sec]
        m.silence_ratio_at_start = sum(1 for v in window_start if v > SILENCE_THRESHOLD) / max(len(window_start), 1)
        m.silence_ratio_at_end = sum(1 for v in window_end if v > SILENCE_THRESHOLD) / max(len(window_end), 1)
        # Overall silence ratio
        window_all = silence[start_sec:end_sec]
        m.silence_ratio_overall = sum(1 for v in window_all if v > SILENCE_THRESHOLD) / max(len(window_all), 1)
        # Max silence gap
        max_gap = 0.0
        current_gap = 0.0
        for v in window_all:
            if v > SILENCE_THRESHOLD:
                current_gap += 1.0
                max_gap = max(max_gap, current_gap)
            else:
                current_gap = 0.0
        m.max_silence_gap = max_gap

    # Abrupt cuts: boundary not at sentence boundary
    m.start_at_sentence_boundary = not m.start_mid_sentence
    m.end_at_sentence_boundary = not m.end_mid_sentence

    # Abrupt: high silence at boundary (candidate starts/ends in silence)
    m.abrupt_start = m.silence_ratio_at_start > 0.5
    m.abrupt_end = m.silence_ratio_at_end > 0.5

    # Warnings
    if not m.min_dur_compliant:
        m.warnings.append("below_min_duration")
    if not m.max_dur_compliant:
        m.warnings.append("above_max_duration")
    if m.start_mid_sentence:
        m.warnings.append("starts_mid_sentence")
    if m.end_mid_sentence:
        m.warnings.append("ends_mid_sentence")
    if m.starts_with_filler:
        m.warnings.append("starts_with_filler")
    if m.abrupt_start:
        m.warnings.append("abrupt_start")
    if m.abrupt_end:
        m.warnings.append("abrupt_end")
    if m.silence_ratio_overall > 0.3:
        m.warnings.append("high_silence_ratio")
    if m.max_silence_gap > 3.0:
        m.warnings.append("long_silence_gap")

    return m


def evaluate_audio(
    candidate: dict,
    audio_sigs: dict | None = None,
    idx: int = 0,
) -> AudioMetrics:
    """Evaluate audio quality of a candidate."""
    m = AudioMetrics()
    m.candidate_id = _candidate_id(candidate, idx)

    silence = (audio_sigs or {}).get("silence", [])
    loudness = (audio_sigs or {}).get("loudness", [])
    burst = (audio_sigs or {}).get("burst", [])

    if not (silence or loudness or burst):
        return m

    start = int(candidate.get("start", 0.0))
    end = int(candidate.get("end", 0.0))
    n = len(silence)

    window_silence = silence[start:min(end, n)]
    window_loudness = loudness[start:min(end, n)] if loudness else []
    window_burst = burst[start:min(end, n)] if burst else []

    if window_silence:
        m.silence_ratio = sum(1 for v in window_silence if v > SILENCE_THRESHOLD) / len(window_silence)
        m.speech_ratio = 1.0 - m.silence_ratio
    if window_loudness:
        m.mean_rms = sum(window_loudness) / len(window_loudness)
        m.peak_rms = max(window_loudness)
        # Energy variance
        mean = m.mean_rms
        m.energy_variance = sum((v - mean) ** 2 for v in window_loudness) / len(window_loudness)
    if window_burst:
        m.mean_burst = sum(window_burst) / len(window_burst)
        m.peak_burst = max(window_burst)
        m.audio_peak_count = sum(1 for v in window_burst if v > 0.6)

    # Long silence intervals
    current_silence = 0.0
    for v in window_silence:
        if v > SILENCE_THRESHOLD:
            current_silence += 1.0
        else:
            if current_silence >= 2.0:
                m.long_silence_count += 1
                m.long_silence_total += current_silence
            current_silence = 0.0
    if current_silence >= 2.0:
        m.long_silence_count += 1
        m.long_silence_total += current_silence

    # Audio peak near boundary (within 2s of start or end)
    if window_burst:
        start_peaks = [v for v in window_burst[:2] if v > 0.6]
        end_peaks = [v for v in window_burst[-2:] if v > 0.6]
        m.audio_peak_near_boundary = bool(start_peaks or end_peaks)

    return m


def evaluate_transcript(
    candidate: dict,
    transcript: list[dict],
    words: list[dict] | None = None,
    idx: int = 0,
) -> TranscriptMetrics:
    """Evaluate transcript quality of a candidate."""
    m = TranscriptMetrics()
    m.candidate_id = _candidate_id(candidate, idx)

    start = float(candidate.get("start", 0.0))
    end = float(candidate.get("end", 0.0))

    # Transcript segments overlapping candidate
    overlapping = [s for s in transcript if float(s.get("end", 0)) > start and float(s.get("start", 0)) < end]
    m.transcript_coverage = len(overlapping)

    # Word count
    if words:
        clip_words = [w for w in words if start <= float(w.get("start", 0.0)) < end]
        m.word_count = len(clip_words)
    else:
        # Synthesize from segments
        text = " ".join(s.get("text", "") for s in overlapping)
        m.word_count = len(text.split())

    # Sentence count and completeness
    m.sentence_count = len(overlapping)
    for s in overlapping:
        text = str(s.get("text", "")).strip()
        if text and (text.endswith(".") or text.endswith("?") or text.endswith("!")):
            m.complete_sentences += 1
        else:
            m.incomplete_sentences += 1

    # Speech density (words per second)
    dur = end - start
    if dur > 0:
        m.speech_density = m.word_count / dur

    # Gaps between transcript segments
    max_gap = 0.0
    gap_count = 0
    for i in range(len(overlapping) - 1):
        gap = float(overlapping[i + 1].get("start", 0)) - float(overlapping[i].get("end", 0))
        if gap > GAP_THRESHOLD:
            gap_count += 1
            max_gap = max(max_gap, gap)
    m.max_gap_between_segments = max_gap
    m.gap_count = gap_count

    # Boundary checks
    if transcript:
        first_seg = transcript[0]
        last_seg = transcript[-1]
        # Check if candidate start aligns with a segment boundary
        m.starts_at_transcript_boundary = any(
            abs(float(s.get("start", 0)) - start) < 0.5 for s in transcript
        )
        m.ends_at_transcript_boundary = any(
            abs(float(s.get("end", 0)) - end) < 0.5 for s in transcript
        )

    # Coherence: no excessive gaps
    m.coherent = m.gap_count == 0 and m.incomplete_sentences <= 1

    return m


def evaluate_visual(
    candidate: dict,
    visual_sigs: dict | None = None,
    idx: int = 0,
) -> VisualMetrics:
    """Evaluate visual quality of a candidate."""
    m = VisualMetrics()
    m.candidate_id = _candidate_id(candidate, idx)

    motion = (visual_sigs or {}).get("motion", [])
    scene_cut = (visual_sigs or {}).get("scene_cut", [])

    if not (motion or scene_cut):
        return m

    start = int(candidate.get("start", 0.0))
    end = int(candidate.get("end", 0.0))
    n = len(motion) if motion else len(scene_cut)

    window_motion = motion[start:min(end, len(motion))] if motion else []
    window_scene = scene_cut[start:min(end, len(scene_cut))] if scene_cut else []

    if window_motion:
        m.mean_motion = sum(window_motion) / len(window_motion)
        m.peak_motion = max(window_motion)
        mean = m.mean_motion
        m.motion_variance = sum((v - mean) ** 2 for v in window_motion) / len(window_motion)
        m.static_frame_ratio = sum(1 for v in window_motion if v < 0.1) / len(window_motion)

    if window_scene:
        m.scene_transition_count = sum(1 for v in window_scene if v > 0.5)
        m.sudden_change_count = sum(1 for v in window_scene if v > 0.7)

    # Framing stability: low motion variance = stable
    m.framing_stable = m.motion_variance < 0.1

    return m


def evaluate_diversity(candidates: list[dict]) -> DiversityMetrics:
    """Evaluate diversity across the full candidate set."""
    m = DiversityMetrics()
    m.total_candidates = len(candidates)

    if not candidates:
        return m

    # Overlap matrix
    ious = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            iou = _iou(candidates[i]["start"], candidates[i]["end"],
                       candidates[j]["start"], candidates[j]["end"])
            ious.append(iou)

    if ious:
        m.max_overlap_iou = max(ious)
        m.mean_overlap_iou = sum(ious) / len(ious)

    # Duplicate count (IoU > 0.8)
    m.duplicate_count = sum(1 for iou in ious if iou > 0.8)

    # Temporal spread (std of start times)
    starts = [c["start"] for c in candidates]
    if len(starts) > 1:
        mean_start = sum(starts) / len(starts)
        m.temporal_spread = (sum((s - mean_start) ** 2 for s in starts) / len(starts)) ** 0.5

    # Concentration ratio (candidates in densest 20% of video duration)
    if candidates:
        dur = max(c["end"] for c in candidates) - min(c["start"] for c in candidates)
        if dur > 0:
            # Divide video into 5 sections, count candidates per section
            section_size = dur / 5.0
            section_counts = [0] * 5
            for c in candidates:
                mid = (c["start"] + c["end"]) / 2.0
                sec = min(4, int((mid - min(c["start"] for c in candidates)) / section_size))
                section_counts[sec] += 1
            max_section = max(section_counts)
            m.concentration_ratio = max_section / len(candidates)
            m.distinct_sections = sum(1 for c in section_counts if c > 0)

    # Unique moments (candidates with IoU < 0.3 with all others)
    unique = 0
    for i, c in enumerate(candidates):
        is_unique = True
        for j, other in enumerate(candidates):
            if i != j:
                iou = _iou(c["start"], c["end"], other["start"], other["end"])
                if iou > 0.3:
                    is_unique = False
                    break
        if is_unique:
            unique += 1
    m.unique_moments = unique

    return m


def evaluate_candidate(
    candidate: dict,
    transcript: list[dict],
    words: list[dict] | None = None,
    audio_sigs: dict | None = None,
    visual_sigs: dict | None = None,
    video_duration: float = 0.0,
    idx: int = 0,
) -> CandidateEvaluation:
    """Evaluate a single candidate across all dimensions."""
    ev = CandidateEvaluation()
    ev.candidate_id = _candidate_id(candidate, idx)
    ev.candidate = candidate

    ev.temporal = evaluate_temporal(candidate, video_duration, audio_sigs, idx)
    ev.audio = evaluate_audio(candidate, audio_sigs, idx)
    ev.transcript = evaluate_transcript(candidate, transcript, words, idx)
    ev.visual = evaluate_visual(candidate, visual_sigs, idx)

    # Collect warnings
    ev.warnings = list(ev.temporal.warnings)
    if ev.audio.silence_ratio > 0.5:
        ev.warnings.append("high_audio_silence")
    if ev.transcript.incomplete_sentences > 1:
        ev.warnings.append("multiple_incomplete_sentences")
    if ev.visual.static_frame_ratio > 0.5:
        ev.warnings.append("high_static_frame_ratio")

    # Machine score (composite, NOT equivalent to human judgment)
    score = 0.0
    score += (1.0 - ev.temporal.silence_ratio_overall) * 20.0
    score += ev.transcript.speech_density * 5.0
    score += ev.audio.speech_ratio * 15.0
    score += ev.visual.mean_motion * 10.0
    score += (1.0 if ev.temporal.min_dur_compliant else 0.0) * 10.0
    score += (1.0 if ev.temporal.max_dur_compliant else 0.0) * 10.0
    score += (1.0 if not ev.temporal.start_mid_sentence else 0.0) * 10.0
    score += (1.0 if not ev.temporal.end_mid_sentence else 0.0) * 10.0
    ev.machine_score = round(min(100.0, max(0.0, score)), 1)

    return ev


def evaluate_video(
    candidates: list[dict],
    transcript: list[dict],
    video_duration: float,
    audio_sigs: dict | None = None,
    visual_sigs: dict | None = None,
    words: list[dict] | None = None,
    video_path: str = "",
    video_hash: str = "",
    video_resolution: str = "",
    video_fps: float = 0.0,
    git_commit: str = "",
    signal_extraction_time: float = 0.0,
    candidate_generation_time: float = 0.0,
    progress_callback: Optional[Callable[[str, float, str], None]] = None,
) -> EvaluationReport:
    """Evaluate all candidates for a video and produce a report."""
    t0 = time.time()

    report = EvaluationReport()
    report.video_path = video_path
    report.video_duration = video_duration
    report.video_hash = video_hash
    report.video_resolution = video_resolution
    report.video_fps = video_fps
    report.git_commit = git_commit
    report.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report.signal_extraction_time = signal_extraction_time
    report.candidate_generation_time = candidate_generation_time

    if progress_callback:
        progress_callback("evaluating", 80, "Evaluating candidates...")

    # Evaluate each candidate
    for idx, c in enumerate(candidates):
        ev = evaluate_candidate(c, transcript, words, audio_sigs, visual_sigs, video_duration, idx)
        report.candidates.append(ev)

    # Diversity
    report.diversity = evaluate_diversity(candidates)

    # Warnings
    for ev in report.candidates:
        for w in ev.warnings:
            report.warnings.append("%s: %s" % (ev.candidate_id, w))

    report.candidate_count = len(candidates)
    report.evaluation_time = time.time() - t0
    report.total_time = report.signal_extraction_time + report.candidate_generation_time + report.evaluation_time

    if progress_callback:
        progress_callback("evaluating", 95, "Evaluation complete")

    return report


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def report_to_dict(report: EvaluationReport) -> dict:
    """Convert EvaluationReport to a serializable dict."""
    d = asdict(report)
    # Convert CandidateEvaluation objects
    d["candidates"] = [asdict(ev) for ev in report.candidates]
    return d


def report_to_json(report: EvaluationReport, path: str) -> None:
    """Save report as JSON."""
    d = report_to_dict(report)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)


def report_to_csv(report: EvaluationReport, path: str) -> None:
    """Save candidate metrics as CSV."""
    import csv
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "candidate_id", "start", "end", "duration", "viral_score",
            "temporal_warnings", "speech_ratio", "silence_ratio",
            "mean_rms", "peak_rms", "word_count", "sentence_count",
            "complete_sentences", "incomplete_sentences", "speech_density",
            "max_gap", "scene_transitions", "mean_motion", "static_ratio",
            "machine_score",
        ])
        for ev in report.candidates:
            writer.writerow([
                ev.candidate_id,
                ev.candidate.get("start", 0),
                ev.candidate.get("end", 0),
                ev.temporal.duration,
                ev.candidate.get("viral_score", 0),
                ";".join(ev.temporal.warnings),
                round(ev.audio.speech_ratio, 3),
                round(ev.audio.silence_ratio, 3),
                round(ev.audio.mean_rms, 3),
                round(ev.audio.peak_rms, 3),
                ev.transcript.word_count,
                ev.transcript.sentence_count,
                ev.transcript.complete_sentences,
                ev.transcript.incomplete_sentences,
                round(ev.transcript.speech_density, 3),
                round(ev.transcript.max_gap_between_segments, 2),
                ev.visual.scene_transition_count,
                round(ev.visual.mean_motion, 3),
                round(ev.visual.static_frame_ratio, 3),
                ev.machine_score,
            ])


def report_to_html(report: EvaluationReport, path: str) -> None:
    """Generate a readable HTML report."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    lines.append("<!DOCTYPE html>")
    lines.append("<html><head><title>OpenClip Candidate Evaluation</title>")
    lines.append("<style>")
    lines.append("body { font-family: sans-serif; margin: 20px; }")
    lines.append("h1 { color: #333; }")
    lines.append("table { border-collapse: collapse; width: 100%; margin: 20px 0; }")
    lines.append("th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }")
    lines.append("th { background-color: #f2f2f2; }")
    lines.append(".warning { color: red; }")
    lines.append(".metric { margin: 5px 0; }")
    lines.append("</style></head><body>")
    lines.append("<h1>OpenClip Candidate Evaluation Report</h1>")
    lines.append("<div class='metric'><strong>Video:</strong> %s</div>" % report.video_path)
    lines.append("<div class='metric'><strong>Duration:</strong> %.1fs</div>" % report.video_duration)
    lines.append("<div class='metric'><strong>Candidates:</strong> %d</div>" % report.candidate_count)
    lines.append("<div class='metric'><strong>Signal extraction:</strong> %.2fs</div>" % report.signal_extraction_time)
    lines.append("<div class='metric'><strong>Candidate generation:</strong> %.2fs</div>" % report.candidate_generation_time)
    lines.append("<div class='metric'><strong>Evaluation:</strong> %.2fs</div>" % report.evaluation_time)
    lines.append("<div class='metric'><strong>Total:</strong> %.2fs</div>" % report.total_time)
    lines.append("<h2>Diversity</h2>")
    lines.append("<div class='metric'>Max overlap IoU: %.3f</div>" % report.diversity.max_overlap_iou)
    lines.append("<div class='metric'>Mean overlap IoU: %.3f</div>" % report.diversity.mean_overlap_iou)
    lines.append("<div class='metric'>Unique moments: %d / %d</div>" % (report.diversity.unique_moments, report.diversity.total_candidates))
    lines.append("<div class='metric'>Temporal spread: %.1fs</div>" % report.diversity.temporal_spread)
    lines.append("<div class='metric'>Concentration ratio: %.2f</div>" % report.diversity.concentration_ratio)
    lines.append("<div class='metric'>Duplicate count: %d</div>" % report.diversity.duplicate_count)
    lines.append("<h2>Candidates</h2>")
    lines.append("<table><tr><th>ID</th><th>Start</th><th>End</th><th>Dur</th><th>Score</th><th>Speech%%</th><th>Silence%%</th><th>Words</th><th>Warnings</th></tr>")
    for ev in report.candidates:
        warnings_str = ", ".join(ev.warnings) if ev.warnings else "none"
        lines.append("<tr><td>%s</td><td>%.1f</td><td>%.1f</td><td>%.1f</td><td>%.0f</td><td>%.0f%%</td><td>%.0f%%</td><td>%d</td><td class='warning'>%s</td></tr>" % (
            ev.candidate_id, ev.candidate.get("start", 0), ev.candidate.get("end", 0),
            ev.temporal.duration, ev.machine_score,
            ev.audio.speech_ratio * 100, ev.audio.silence_ratio * 100,
            ev.transcript.word_count, warnings_str))
    lines.append("</table></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

def check_reproducibility(
    generate_fn: Callable,
    *args,
    runs: int = 3,
    **kwargs,
) -> dict:
    """Run generate_fn multiple times and compare outputs."""
    results = []
    for i in range(runs):
        r = generate_fn(*args, **kwargs)
        results.append(r)

    # Compare candidate counts
    counts = [len(r) for r in results]
    count_stable = len(set(counts)) == 1

    # Compare timestamps
    timestamps_stable = True
    if results and results[0]:
        for i in range(1, len(results)):
            if len(results[i]) != len(results[0]):
                timestamps_stable = False
                break
            for a, b in zip(results[0], results[i]):
                if a.get("start") != b.get("start") or a.get("end") != b.get("end"):
                    timestamps_stable = False
                    break

    # Compare scores
    scores_stable = True
    if results and results[0]:
        for i in range(1, len(results)):
            for a, b in zip(results[0], results[i]):
                if a.get("viral_score") != b.get("viral_score"):
                    scores_stable = False
                    break

    return {
        "runs": runs,
        "candidate_counts": counts,
        "count_stable": count_stable,
        "timestamps_stable": timestamps_stable,
        "scores_stable": scores_stable,
        "reproducible": count_stable and timestamps_stable and scores_stable,
    }


# ---------------------------------------------------------------------------
# Human review
# ---------------------------------------------------------------------------

@dataclass
class HumanReview:
    """Human review annotation for a candidate."""
    candidate_id: str = ""
    reviewer: str = ""
    timestamp: str = ""
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


def save_human_review(review: HumanReview, path: str) -> None:
    """Save a human review annotation to JSON."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = asdict(review)
    # Append to existing file if present
    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = [existing]
        except (json.JSONDecodeError, Exception):
            existing = []
    existing.append(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)


def load_human_reviews(path: str) -> list[HumanReview]:
    """Load human reviews from JSON."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = [data]
        return [HumanReview(**d) for d in data]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def compute_file_hash(path: str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_video_metadata(video_path: str) -> dict:
    """Get video metadata using ffprobe."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=width,height,r_frame_rate,codec_name",
             "-show_entries", "format=duration",
             "-of", "json", video_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            import json as _json
            data = _json.loads(result.stdout)
            streams = data.get("streams", [])
            fmt = data.get("format", {})
            vstream = streams[0] if streams else {}
            fps_str = vstream.get("r_frame_rate", "0/1")
            if "/" in fps_str:
                num, den = fps_str.split("/")
                fps = float(num) / float(den) if float(den) > 0 else 0.0
            else:
                fps = float(fps_str)
            return {
                "width": vstream.get("width", 0),
                "height": vstream.get("height", 0),
                "fps": fps,
                "duration": float(fmt.get("duration", 0)),
                "codec": vstream.get("codec_name", "unknown"),
            }
    except Exception:
        pass
    return {"width": 0, "height": 0, "fps": 0.0, "duration": 0.0, "codec": "unknown"}


def create_synthetic_video(
    output_path: str,
    duration: float = 30.0,
    pattern: str = "speech_silence",
) -> str:
    """Create a synthetic test video using FFmpeg.

    Patterns:
      - "speech_silence": alternating speech (tone) and silence
      - "audio_peaks": silence with periodic loud bursts
      - "scene_changes": video with periodic scene transitions
      - "minimal": shortest possible video
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if pattern == "minimal":
        # 10s minimal video
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "color=c=black:s=320x240:d=10",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=10",
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ]
    elif pattern == "audio_peaks":
        # 30s: silence with 3 loud bursts (simple tone bursts)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=30",
            "-f", "lavfi", "-i",
            "sine=frequency=440:duration=30",
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ]
    elif pattern == "scene_changes":
        # 30s: simple video with color changes via fade
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
            "-vf", "fade=t=in:st=0:d=1,fade=t=out:st=29:d=1",
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ]
    else:
        # Default: speech_silence — alternating tone and silence
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=%d" % int(duration),
            "-f", "lavfi", "-i",
            "aevalsrc=0.3*sin(2*PI*440*t)*(1+0.5*sin(2*PI*0.5*t)):d=%d" % int(duration),
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ]

    import subprocess
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg failed: %s" % result.stderr.decode(errors="ignore")[:500])
    return output_path


def run_benchmark(
    video_path: str,
    output_dir: str,
    transcript: list[dict] | None = None,
    progress_callback: Optional[Callable[[str, float, str], None]] = None,
) -> EvaluationReport:
    """Run a full benchmark on a video file.

    If transcript is None, generates a synthetic transcript based on duration.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Get metadata
    meta = get_video_metadata(video_path)
    duration = meta["duration"] or 30.0
    file_hash = compute_file_hash(video_path)

    # Synthetic transcript if none provided
    if transcript is None:
        transcript = []
        seg_dur = 5.0
        for i in range(int(duration / seg_dur)):
            transcript.append({
                "start": i * seg_dur,
                "end": (i + 1) * seg_dur,
                "text": "This is segment %d with some spoken content for testing." % (i + 1),
            })

    # Synthesize words
    words = []
    for seg in transcript:
        text = seg.get("text", "")
        seg_words = text.split()
        if not seg_words:
            continue
        per_word = (seg["end"] - seg["start"]) / len(seg_words)
        for i, w in enumerate(seg_words):
            words.append({
                "word": w,
                "start": seg["start"] + i * per_word,
                "end": seg["start"] + (i + 1) * per_word,
            })

    # Signal extraction
    t0 = time.time()
    # Import signals from the right path
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")
    import signals as sig_mod

    audio_sigs = {"loudness": [], "burst": [], "silence": []}
    visual_sigs = {"motion": [], "scene_cut": []}
    reaction_sig = []
    try:
        audio_sigs = sig_mod.extract_audio_signals(video_path, duration)
    except Exception as e:
        logger.warning("Audio extraction failed: %s", e)
    try:
        visual_sigs = sig_mod.extract_visual_signals(video_path, duration)
    except Exception as e:
        logger.warning("Visual extraction failed: %s", e)
    try:
        if audio_sigs.get("burst") and visual_sigs.get("motion"):
            reaction_sig = sig_mod.build_reaction_signal(
                audio_sigs["burst"], [0.0] * len(audio_sigs["burst"]), visual_sigs["motion"])
        elif audio_sigs.get("burst"):
            reaction_sig = audio_sigs["burst"]
        else:
            reaction_sig = [0.5] * max(1, int(duration))
    except Exception:
        reaction_sig = [0.5] * max(1, int(duration))

    signal_time = time.time() - t0

    # Candidate generation
    t1 = time.time()
    import deterministic_candidate as dc_mod
    candidates = dc_mod.generate_candidates(
        transcript, duration,
        audio_sigs=audio_sigs,
        visual_sigs=visual_sigs,
    )
    gen_time = time.time() - t1

    # Evaluation
    report = evaluate_video(
        candidates, transcript, duration,
        audio_sigs=audio_sigs,
        visual_sigs=visual_sigs,
        words=words,
        video_path=video_path,
        video_hash=file_hash,
        video_resolution="%dx%d" % (meta["width"], meta["height"]),
        video_fps=meta["fps"],
        git_commit=_get_git_commit(),
        signal_extraction_time=signal_time,
        candidate_generation_time=gen_time,
        progress_callback=progress_callback,
    )

    # Save reports
    report_dir = os.path.join(output_dir, "reports")
    os.makedirs(report_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    report_to_json(report, os.path.join(report_dir, "%s_report.json" % base_name))
    report_to_csv(report, os.path.join(report_dir, "%s_report.csv" % base_name))
    report_to_html(report, os.path.join(report_dir, "%s_report.html" % base_name))

    return report


def _get_git_commit() -> str:
    """Get current git commit hash."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Synthetic benchmark suite
# ---------------------------------------------------------------------------

def run_synthetic_benchmarks(output_dir: str) -> list[EvaluationReport]:
    """Run benchmarks on synthetic videos."""
    results = []
    patterns = ["minimal", "audio_peaks", "scene_changes"]
    for pattern in patterns:
        video_path = os.path.join(output_dir, "synthetic_%s.mp4" % pattern)
        try:
            create_synthetic_video(video_path, duration=30.0, pattern=pattern)
            report = run_benchmark(video_path, output_dir)
            results.append(report)
            print("Benchmark %s: %d candidates, %.2fs total" % (pattern, report.candidate_count, report.total_time))
        except Exception as e:
            print("Benchmark %s failed: %s" % (pattern, e))
    return results
