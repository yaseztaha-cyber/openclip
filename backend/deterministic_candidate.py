"""
deterministic_candidate.py — Local, deterministic clip candidate generation.

Replaces the LLM-dependent get_clip_suggestions() with a fully local
candidate-generation engine.  No network calls, no LLM, no paid APIs.

Reuses existing deterministic functions:
  - editorial.build_sentences()
  - editorial.all_words()
  - editorial.snap_bounds()
  - editorial.strip_leading_fillers()
  - editorial.deterministic_score()
  - editorial.select_clips()
  - scoring.score_audio_candidate()
  - scoring.score_visual_candidate()
  - scoring.score_hook_first_3s()
  - signals (audio_sigs, visual_sigs passed in)

Candidate sources:
  1. Sentence-aligned windows (each sentence >= min_dur)
  2. Consecutive sentence-pair windows (setup -> payoff)
  3. Sliding windows at sentence boundaries (15s, 30s, 45s, 60s)
  4. Audio-peak windows (centered on audio burst peaks)
  5. Scene-cut windows (starting at scene transitions)

Output format matches LLM suggestions: list of dicts with
  start, end, title, reason, viral_score, hook, payoff, curiosity,
  information, narrative, generation_source, boundary_info, warnings
"""

from __future__ import annotations

import logging
import math
from typing import Callable, Optional

import editorial  # noqa: E402
import scoring  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_CLIP_DUR = 10.0
MAX_CLIP_DUR = 120.0
TARGET_DURATIONS = (15.0, 30.0, 45.0, 60.0)
MAX_CANDIDATES = 60  # bound candidate generation
MAX_OVERLAP_IOU = 0.6  # drop candidates with IoU above this


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthesize_words(transcript: list[dict]) -> list[dict]:
    """Synthesize word-level timestamps if transcript segments lack them.

    Splits segment text on whitespace and distributes time evenly.
    """
    words: list[dict] = []
    for seg in transcript:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        seg_words = text.split()
        if not seg_words:
            continue
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", seg_start + 1.0))
        seg_dur = max(0.1, seg_end - seg_start)
        per_word = seg_dur / len(seg_words)
        for i, w in enumerate(seg_words):
            ws = seg_start + i * per_word
            we = ws + per_word
            words.append({"word": w, "start": ws, "end": we})
    return words


def _get_words(transcript: list[dict]) -> list[dict]:
    """Get word list from transcript, synthesizing if needed."""
    import editorial as _ed
    words = _ed.all_words(transcript)
    if not words:
        words = _synthesize_words(transcript)
    return words


def _clip_text(transcript: list[dict], start: float, end: float) -> str:
    """Extract transcript text for a time window."""
    parts = []
    for seg in transcript:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        if seg_end > start and seg_start < end:
            parts.append(str(seg.get("text", "")).strip())
    return " ".join(p for p in parts if p)


def _iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Intersection over union for two time windows."""
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = (a_end - a_start) + (b_end - b_start) - inter
    if union <= 0:
        return 0.0
    return inter / union


def _dedup_by_overlap(candidates: list[dict], max_iou: float = MAX_OVERLAP_IOU) -> list[dict]:
    """Remove candidates that overlap too much with a stronger candidate."""
    if not candidates:
        return []
    # Sort by score descending (stronger first), then by start
    ordered = sorted(candidates, key=lambda c: (-c.get("_score", 0), c.get("start", 0)))
    kept: list[dict] = []
    for c in ordered:
        if any(_iou(c["start"], c["end"], k["start"], k["end"]) > max_iou for k in kept):
            continue
        kept.append(c)
    kept.sort(key=lambda c: c["start"])
    return kept


def _make_candidate(
    start: float,
    end: float,
    transcript: list[dict],
    words: list[dict],
    source: str,
    title_hint: str = "",
) -> dict | None:
    """Build a candidate dict in LLM-compatible format."""
    dur = end - start
    if dur < MIN_CLIP_DUR or dur > MAX_CLIP_DUR:
        return None
    if start < 0 or end <= start:
        return None

    text = _clip_text(transcript, start, end)
    if not text.strip():
        return None

    # Detect incomplete dialogue
    warnings: list[str] = []
    # Check if start is mid-sentence (previous segment continues)
    for seg in transcript:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        if seg_start < start < seg_end:
            warnings.append("start_mid_sentence")
            break
    for seg in transcript:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        if seg_start < end < seg_end:
            warnings.append("end_mid_sentence")
            break

    # Check if first word is a filler
    clip_words = [w for w in words if start <= float(w.get("start", 0.0)) < end]
    if clip_words:
        first_word = str(clip_words[0].get("word", "")).lower().strip(".,!?")
        fillers = {"um", "uh", "so", "well", "like", "okay", "right", "anyway"}
        if first_word in fillers:
            warnings.append("starts_with_filler")

    # Score using deterministic_score
    import editorial as _ed
    try:
        det = _ed.deterministic_score(text, words, start, end, 0.0)
        score_total = det.get("total", 50.0)
    except Exception:
        score_total = 50.0
        det = {"total": 50.0, "parts": {}}

    # Generate title from first meaningful words
    title = title_hint
    if not title:
        title_words = text.split()[:6]
        title = " ".join(title_words)
        if len(title) > 60:
            title = title[:57] + "..."

    # Build reason
    reasons = []
    if "hook_pattern" in det.get("parts", {}) and det["parts"]["hook_pattern"] > 0:
        reasons.append("hook")
    if "clean_ending" in det.get("parts", {}) and det["parts"]["clean_ending"] > 0:
        reasons.append("clean ending")
    if "info_density" in det.get("parts", {}) and det["parts"]["info_density"] > 5:
        reasons.append("high info density")
    reason = "Deterministic candidate: " + (", ".join(reasons) if reasons else "transcript window")

    # Determine boundary info
    boundary_info = {
        "start_snapped": False,
        "end_snapped": False,
        "incomplete_start": "start_mid_sentence" in warnings,
        "incomplete_end": "end_mid_sentence" in warnings,
    }

    candidate = {
        "start": round(start, 3),
        "end": round(end, 3),
        "title": title,
        "reason": reason,
        "viral_score": max(1, min(10, int(score_total / 10))),
        "hook": max(1, min(10, int(det.get("parts", {}).get("hook_pattern", 0) / 2 + 3))),
        "payoff": max(1, min(10, int(det.get("parts", {}).get("clean_ending", 0) / 3 + 5))),
        "curiosity": max(1, min(10, int(det.get("parts", {}).get("punctuation", 0) / 3 + 4))),
        "information": max(1, min(10, int(det.get("parts", {}).get("info_density", 0) / 2 + 3))),
        "narrative": max(1, min(10, int(score_total / 12 + 2))),
        "generation_source": "deterministic",
        "boundary_info": boundary_info,
        "warnings": warnings,
        "_score": score_total,
        "_det": det,
    }
    return candidate


# ---------------------------------------------------------------------------
# Candidate generators
# ---------------------------------------------------------------------------

def _gen_sentence_windows(
    sentences: list[dict],
    transcript: list[dict],
    words: list[dict],
    min_dur: float,
    max_dur: float,
) -> list[dict]:
    """Generate candidates from individual sentences and sentence pairs."""
    cands: list[dict] = []

    # Individual sentences (if long enough)
    for s in sentences:
        dur = s["end"] - s["start"]
        if dur >= min_dur and dur <= max_dur:
            c = _make_candidate(s["start"], s["end"], transcript, words, "sentence")
            if c:
                cands.append(c)

    # Consecutive sentence pairs (setup -> payoff)
    for i in range(len(sentences) - 1):
        start = sentences[i]["start"]
        end = sentences[i + 1]["end"]
        dur = end - start
        if min_dur <= dur <= max_dur:
            c = _make_candidate(start, end, transcript, words, "sentence_pair")
            if c:
                cands.append(c)

    # Three-sentence windows (context -> buildup -> payoff)
    for i in range(len(sentences) - 2):
        start = sentences[i]["start"]
        end = sentences[i + 2]["end"]
        dur = end - start
        if min_dur <= dur <= max_dur:
            c = _make_candidate(start, end, transcript, words, "sentence_triple")
            if c:
                cands.append(c)

    return cands


def _gen_sliding_windows(
    sentences: list[dict],
    transcript: list[dict],
    words: list[dict],
    video_duration: float,
    min_dur: float,
    max_dur: float,
) -> list[dict]:
    """Generate sliding windows anchored at sentence boundaries."""
    cands: list[dict] = []
    if not sentences:
        return cands

    # Anchor points: sentence starts
    anchors = [s["start"] for s in sentences]

    for anchor in anchors:
        for target_dur in TARGET_DURATIONS:
            if target_dur < min_dur or target_dur > max_dur:
                continue
            end = anchor + target_dur
            if end > video_duration:
                end = video_duration
            dur = end - anchor
            if dur < min_dur:
                continue
            # Snap end to nearest sentence boundary
            import editorial as _ed
            ns, ne = _ed.snap_bounds(anchor, end, sentences, words)
            if ne - ns < min_dur:
                continue
            if ne - ns > max_dur:
                ne = ns + max_dur
            c = _make_candidate(ns, ne, transcript, words, "sliding")
            if c:
                cands.append(c)

    return cands


def _gen_audio_peak_windows(
    audio_sigs: dict,
    sentences: list[dict],
    transcript: list[dict],
    words: list[dict],
    video_duration: float,
    min_dur: float,
    max_dur: float,
) -> list[dict]:
    """Generate windows centered on audio burst peaks."""
    cands: list[dict] = []
    burst = audio_sigs.get("burst", [])
    if not burst:
        return cands

    # Find peaks (local maxima above 0.6)
    peaks: list[float] = []
    for i in range(1, len(burst) - 1):
        if burst[i] > 0.6 and burst[i] >= burst[i - 1] and burst[i] >= burst[i + 1]:
            peaks.append(float(i))
    # Also include absolute high points
    for i, v in enumerate(burst):
        if v > 0.8 and float(i) not in peaks:
            peaks.append(float(i))

    # Dedup peaks (within 5s)
    deduped_peaks: list[float] = []
    for p in sorted(peaks):
        if not deduped_peaks or p - deduped_peaks[-1] > 5.0:
            deduped_peaks.append(p)

    for peak in deduped_peaks[:20]:  # bound
        for target_dur in (20.0, 30.0, 45.0):
            if target_dur < min_dur or target_dur > max_dur:
                continue
            # Center window on peak
            half = target_dur / 2.0
            start = max(0.0, peak - half)
            end = min(video_duration, peak + half)
            if end - start < min_dur:
                continue
            import editorial as _ed
            ns, ne = _ed.snap_bounds(start, end, sentences, words)
            if ne - ns < min_dur or ne - ns > max_dur:
                continue
            c = _make_candidate(ns, ne, transcript, words, "audio_peak")
            if c:
                cands.append(c)

    return cands


def _gen_scene_cut_windows(
    visual_sigs: dict,
    sentences: list[dict],
    transcript: list[dict],
    words: list[dict],
    video_duration: float,
    min_dur: float,
    max_dur: float,
) -> list[dict]:
    """Generate windows starting at scene transitions."""
    cands: list[dict] = []
    scene_cuts = visual_sigs.get("scene_cut", [])
    if not scene_cuts:
        return cands

    # Find scene cut timestamps (values > 0.5)
    cut_times: list[float] = []
    for i, v in enumerate(scene_cuts):
        if v > 0.5:
            cut_times.append(float(i))

    # Dedup (within 3s)
    deduped: list[float] = []
    for t in sorted(cut_times):
        if not deduped or t - deduped[-1] > 3.0:
            deduped.append(t)

    for cut in deduped[:25]:  # bound
        for target_dur in (15.0, 30.0, 45.0):
            if target_dur < min_dur or target_dur > max_dur:
                continue
            start = cut
            end = min(video_duration, cut + target_dur)
            if end - start < min_dur:
                continue
            import editorial as _ed
            ns, ne = _ed.snap_bounds(start, end, sentences, words)
            if ne - ns < min_dur or ne - ns > max_dur:
                continue
            c = _make_candidate(ns, ne, transcript, words, "scene_cut")
            if c:
                cands.append(c)

    return cands


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_candidates(
    transcript: list[dict],
    video_duration: float,
    audio_sigs: dict | None = None,
    visual_sigs: dict | None = None,
    min_dur: float = MIN_CLIP_DUR,
    max_dur: float = MAX_CLIP_DUR,
    top_k: int = 15,
    progress_callback: Optional[Callable[[str, float, str], None]] = None,
) -> list[dict]:
    """
    Generate deterministic clip candidates from transcript + signals.

    Returns a list of candidate dicts in LLM-compatible format, ranked
    by deterministic score.  No network calls.
    """
    if not transcript:
        logger.warning("Empty transcript — no candidates generated")
        return []

    audio_sigs = audio_sigs or {}
    visual_sigs = visual_sigs or {}

    # Clamp durations
    min_dur = max(5.0, min(min_dur, max_dur))
    max_dur = max(min_dur, min(max_dur, MAX_CLIP_DUR))

    if video_duration <= 0:
        video_duration = max(seg.get("end", 0.0) for seg in transcript) if transcript else 0.0
    if video_duration <= 0:
        logger.warning("No video duration — cannot generate candidates")
        return []

    # Build sentences and words
    import editorial as _ed
    sentences = _ed.build_sentences(transcript)
    words = _get_words(transcript)

    if not sentences:
        logger.warning("No sentences found — cannot generate candidates")
        return []

    if progress_callback:
        progress_callback("analyzing", 55, "Generating deterministic candidates...")

    # Generate from all sources
    all_cands: list[dict] = []

    # 1. Sentence windows
    all_cands.extend(_gen_sentence_windows(sentences, transcript, words, min_dur, max_dur))

    # 2. Sliding windows
    all_cands.extend(_gen_sliding_windows(sentences, transcript, words, video_duration, min_dur, max_dur))

    # 3. Audio peak windows
    all_cands.extend(_gen_audio_peak_windows(audio_sigs, sentences, transcript, words, video_duration, min_dur, max_dur))

    # 4. Scene cut windows
    all_cands.extend(_gen_scene_cut_windows(visual_sigs, sentences, transcript, words, video_duration, min_dur, max_dur))

    if progress_callback:
        progress_callback("analyzing", 60, f"Generated {len(all_cands)} raw candidates")

    # Bound candidate count
    if len(all_cands) > MAX_CANDIDATES:
        # Keep highest scored
        all_cands.sort(key=lambda c: c.get("_score", 0), reverse=True)
        all_cands = all_cands[:MAX_CANDIDATES]

    # Dedup by overlap
    all_cands = _dedup_by_overlap(all_cands, MAX_OVERLAP_IOU)

    # Re-score with hook_first_3s if possible
    for c in all_cands:
        try:
            hook3 = scoring.score_hook_first_3s(c, sentences, words)
            c["_hook_first_3s"] = hook3
        except Exception:
            c["_hook_first_3s"] = {"score": 5.0, "reasons": []}

    # Final ranking: sort by _score descending, then by start ascending (stable tie-break)
    all_cands.sort(key=lambda c: (-c.get("_score", 0), c.get("start", 0)))

    # Clean up internal keys before returning
    for c in all_cands:
        c.pop("_score", None)
        c.pop("_det", None)
        c.pop("_hook_first_3s", None)

    if progress_callback:
        progress_callback("analyzing", 65, f"Selected {min(top_k, len(all_cands))} candidates")

    # Return top_k
    result = all_cands[:top_k]
    result.sort(key=lambda c: c["start"])
    return result