"""
editorial.py — Deterministic editorial intelligence for AutoClip.

Pure functions (no ffmpeg, no network) that implement the "real editor"
workflow around the LLM:

    sentences -> snap bounds -> filler/silence cut plan -> time remap
    -> deterministic hook scoring -> merge/dedupe/rank

Ideas adapted from branmendo/autoclip-core (MIT): cut windows with merge
gaps + removal guardrails, filler-token detection, hook-window heuristics
(first-3s/5s pace, ?/!, filler density, dead-air opening). Rebuilt here for
our transcript shape: segments are dicts {start, end, text, words?} where
words (when present) are [{word, start, end}] in ABSOLUTE video seconds.

Nothing here fakes understanding: LLM scores semantic quality, these
functions enforce structure (boundaries, pacing math, diversity).
"""

import logging
import math
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (conservative = do no harm)
# ---------------------------------------------------------------------------

# Cut kinds
FILLER = "filler"
PAUSE = "pause"

# Filler vocabulary (conservative: only true disfluencies + classic phrases).
# "like" is deliberately excluded — too often meaningful.
FILLER_TOKENS = {"um", "uh", "erm", "ah", "hmm", "er", "mmm"}
FILLER_PHRASES = (("you", "know"), ("i", "mean"), ("sort", "of"))

# Padding around filler cuts so speech doesn't clip (autoclip-core: 75ms).
FILLER_PAD_SEC = 0.075
# Merge cuts separated by <= this gap.
CUT_MERGE_GAP_SEC = 0.2
# Never remove more than this fraction of a clip (autoclip-core guardrail).
MAX_REMOVAL_RATIO = 0.40
# Minimum kept span between cuts (avoid confetti).
MIN_KEEP_SEC = 0.30
# Pause squeeze: pauses longer than this keep only this much (dramatic kept).
PAUSE_SQUEEZE_KEEP_SEC = 0.40
PAUSE_SQUEEZE_MIN_SEC = 1.0
# Boundary snap search radius.
SNAP_RADIUS_SEC = 5.0
# Final selection.
TOP_CLIPS = 8
# Overlap (IoU) above which two candidates merge.
MERGE_IOU = 0.5

_TOKEN_RE = re.compile(r"[^\w']+")
_SENT_END_RE = re.compile(r"[.?!]+[\"')\]]*\s*$")


def _norm_token(word: str) -> str:
    return _TOKEN_RE.sub("", str(word)).lower()


def _is_elongation(token: str, base: str) -> bool:
    """um -> umm, uh -> uhhh (bounded, like autoclip-core)."""
    return (
        len(base) >= 2
        and token.startswith(base)
        and len(token) <= len(base) + 5
    )


def _is_filler_token(token: str) -> bool:
    if token in FILLER_TOKENS:
        return True
    return any(_is_elongation(token, b) for b in FILLER_TOKENS)


# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------

def build_sentences(transcript: list[dict]) -> list[dict]:
    """
    Split transcript segments into sentences with absolute timings.

    Returns [{text, start, end, seg_idx}]. With word timings, boundaries
    follow words; otherwise text is split and time distributed by length.
    """
    sentences: list[dict] = []
    for si, seg in enumerate(transcript):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        words = seg.get("words") or []
        if words:
            buf: list[dict] = []
            for w in words:
                buf.append(w)
                wtext = str(w.get("word", ""))
                if _SENT_END_RE.search(wtext):
                    sentences.append(_pack_sentence(buf, si))
                    buf = []
            if buf:
                sentences.append(_pack_sentence(buf, si))
        else:
            # No word timings: naive split, proportional timing.
            parts = re.split(r"(?<=[.?!])\s+", text)
            parts = [p.strip() for p in parts if p.strip()] or [text]
            total = max(sum(len(p) for p in parts), 1)
            cursor = float(seg.get("start", 0.0))
            span = float(seg.get("end", cursor)) - cursor
            for idx, part in enumerate(parts):
                frac = len(part) / total
                end = cursor + span * frac
                if idx == len(parts) - 1:
                    end = float(seg.get("end", cursor))
                sentences.append({
                    "text": part,
                    "start": cursor,
                    "end": end,
                    "seg_idx": si,
                })
                cursor = end
    return sentences


def _pack_sentence(words: list[dict], seg_idx: int) -> dict:
    text = " ".join(str(w.get("word", "")) for w in words).strip()
    return {
        "text": text,
        "start": float(words[0].get("start", 0.0)),
        "end": float(words[-1].get("end", words[0].get("start", 0.0))),
        "seg_idx": seg_idx,
        "words": words,
    }


def all_words(transcript: list[dict]) -> list[dict]:
    """Flatten transcript to word list (absolute seconds)."""
    out: list[dict] = []
    for seg in transcript:
        for w in (seg.get("words") or []):
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# Boundary snapping
# ---------------------------------------------------------------------------

def snap_bounds(start: float, end: float, sentences: list[dict],
                words: Optional[list[dict]] = None) -> tuple[float, float]:
    """
    Snap (start, end) to natural linguistic boundaries.

    - start moves BACK to the nearest sentence start within SNAP_RADIUS_SEC
      (so context/setup is preserved, never mid-sentence).
    - end moves FORWARD to the nearest sentence end within SNAP_RADIUS_SEC
      (so payoffs complete).
    Falls back to word boundaries, then to segment bounds.
    """
    if not sentences:
        return start, end

    new_start = start
    cands = [s for s in sentences if s["start"] <= start + 0.05 and start - s["start"] <= SNAP_RADIUS_SEC]
    if cands:
        # Prefer the sentence CONTAINING start; else nearest previous.
        containing = [s for s in cands if s["end"] >= start - 0.05]
        pick = max(containing, key=lambda s: s["start"]) if containing else max(cands, key=lambda s: s["start"])
        new_start = min(start, pick["start"])

    new_end = end
    cands = [s for s in sentences if s["end"] >= end - 0.05 and s["end"] - end <= SNAP_RADIUS_SEC]
    if cands:
        containing = [s for s in cands if s["start"] <= end + 0.05]
        pick = min(containing, key=lambda s: s["end"]) if containing else min(cands, key=lambda s: s["end"])
        new_end = max(end, pick["end"])

    if new_end <= new_start:
        return start, end
    return new_start, new_end


def strip_leading_fillers(words: list[dict], start: float) -> float:
    """
    Push start past leading filler tokens (um/uh/...) within the first 1.5s,
    plus a weak discourse marker if it is the very first word.
    Returns possibly-adjusted start. Never moves more than 2.5s.
    """
    ordered = sorted(
        (w for w in words if float(w.get("start", 0.0)) >= start - 0.05),
        key=lambda d: float(d.get("start", 0.0)),
    )
    t = start
    first = True
    for w in ordered:
        ws = float(w.get("start", 0.0))
        if ws > start + 2.5:
            break
        tok = _norm_token(str(w.get("word", "")))
        if _is_filler_token(tok):
            t = max(t, float(w.get("end", ws)))
        elif first and tok in {"so", "and", "but", "well", "ok", "okay", "right", "now", "like"}:
            t = max(t, float(w.get("end", ws)))
        elif ws > start + 1.5:
            break
        else:
            if t > start or ws > start + 0.6:
                break
        first = False
    return t


# ---------------------------------------------------------------------------
# Filler detection -> cut windows
# ---------------------------------------------------------------------------

def detect_filler_cuts(words: list[dict], duration: float) -> list[dict]:
    """
    Find filler regions as cut windows [{start, end, kind}].
    Single tokens + known phrases, padded, merged.
    """
    cuts: list[dict] = []
    toks = [_norm_token(str(w.get("word", ""))) for w in words]
    i = 0
    n = len(words)
    while i < n:
        matched_len = 0
        # phrases first (longest)
        for phrase in (("you", "know"), ("i", "mean"), ("sort", "of")):
            if (i + len(phrase) <= n
                    and all(toks[i + k] == phrase[k] for k in range(len(phrase)))):
                matched_len = len(phrase)
                break
        if matched_len == 0 and _is_filler_token(toks[i]):
            matched_len = 1
        if matched_len:
            s = max(0.0, float(words[i].get("start", 0.0)) - FILLER_PAD_SEC)
            e = float(words[i + matched_len - 1].get("end", s)) + FILLER_PAD_SEC
            if duration > 0:
                e = min(e, duration)
            if e > s + 0.05:
                cuts.append({"start": s, "end": e, "kind": FILLER})
            i += matched_len
        else:
            i += 1
    return merge_cuts(cuts)


def merge_cuts(cuts: list[dict]) -> list[dict]:
    """Sort + merge overlapping/adjacent cut windows (gap <= CUT_MERGE_GAP_SEC)."""
    if not cuts:
        return []
    ordered = sorted(cuts, key=lambda c: (c["start"], c["end"]))
    merged = [dict(ordered[0])]
    for c in ordered[1:]:
        last = merged[-1]
        if c["start"] <= last["end"] + CUT_MERGE_GAP_SEC:
            last["end"] = max(last["end"], c["end"])
            if last.get("kind") != c.get("kind"):
                last["kind"] = "mixed"
        else:
            merged.append(dict(c))
    return merged


def plan_pause_cuts(silences: list[tuple[float, float]]) -> list[dict]:
    """
    Turn silencedetect output into squeeze cuts: pauses longer than
    PAUSE_SQUEEZE_MIN_SEC keep the first PAUSE_SQUEEZE_KEEP_SEC, rest cut.
    Shorter pauses are dramatic — kept whole.
    """
    cuts: list[dict] = []
    for s, e in silences:
        if e - s >= PAUSE_SQUEEZE_MIN_SEC:
            cuts.append({"start": s + PAUSE_SQUEEZE_KEEP_SEC, "end": e, "kind": PAUSE})
    return cuts


def apply_guardrails(cuts: list[dict], duration: float) -> list[dict]:
    """
    Enforce: merge, drop cuts that would shred keeps (< MIN_KEEP_SEC),
    cap total removal at MAX_REMOVAL_RATIO (drop smallest cuts first).
    """
    cuts = merge_cuts([c for c in cuts if c["end"] > c["start"]])
    if not cuts or duration <= 0:
        return cuts

    # Drop cuts that would leave slivers between neighbours.
    bounds = [0.0] + [x for c in cuts for x in (c["start"], c["end"])] + [duration]
    kept: list[dict] = []
    for c in cuts:
        prev_end = kept[-1]["end"] if kept else 0.0
        if c["start"] - prev_end < MIN_KEEP_SEC and kept:
            continue
        kept.append(c)
    cuts = kept

    total = sum(c["end"] - c["start"] for c in cuts)
    budget = duration * MAX_REMOVAL_RATIO
    if total > budget:
        # drop smallest cuts first until within budget
        cuts = sorted(cuts, key=lambda c: c["end"] - c["start"], reverse=True)
        kept2: list[dict] = []
        used = 0.0
        for c in cuts:
            dur = c["end"] - c["start"]
            if used + dur <= budget:
                kept2.append(c)
                used += dur
        cuts = merge_cuts(kept2)
    return cuts


# ---------------------------------------------------------------------------
# Time remap (edited timeline for caption sync)
# ---------------------------------------------------------------------------

def build_time_map(cuts: list[dict]):
    """Return fn(old_t) -> new_t after removing merged cut windows."""
    merged = merge_cuts(cuts)

    def _map(t: float) -> float:
        removed = 0.0
        for c in merged:
            if t >= c["end"]:
                removed += c["end"] - c["start"]
            elif t < c["start"]:
                break
        return max(0.0, t - removed)

    return _map


def remap_words(words: list[dict], cuts: list[dict]) -> list[dict]:
    """Drop words inside cuts, shift the rest. Words keep 'word' text."""
    merged = merge_cuts(cuts)
    if not merged:
        return [dict(w) for w in words]
    time_map = build_time_map(merged)
    out: list[dict] = []
    for w in words:
        ws, we = float(w.get("start", 0.0)), float(w.get("end", 0.0))
        if any(c["start"] <= ws < c["end"] or c["start"] < we <= c["end"] for c in merged):
            continue
        nw = dict(w)
        nw["start"], nw["end"] = time_map(ws), time_map(we)
        if nw["end"] > nw["start"]:
            out.append(nw)
    return out


# ---------------------------------------------------------------------------
# Deterministic hook/quality scoring (signals the LLM can't fake)
# ---------------------------------------------------------------------------

QUESTION_RE = re.compile(r"\?")
EXCLAIM_RE = re.compile(r"!")
HOOK_PATTERN_RES = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bnobody tells you\b", r"\bbiggest mistake\b", r"\bi didn'?t realize\b",
        r"\bhere'?s what happened\b", r"\bchanged everything\b", r"\bthe problem is\b",
        r"\bmost people don'?t\b", r"\bi was (completely |totally )?wrong\b",
        r"\bthere'?s one thing\b", r"\bdon'?t ever\b", r"\bthe reason (why|is)\b",
        r"\byou won'?t believe\b", r"\binteresting part\b", r"\bbut then\b",
        r"\buntil\b.{0,20}\b", r"\bso i tried\b", r"\bturns out\b",
        r"\bhere'?s the thing\b", r"\bwhat if i told you\b", r"\bstop doing\b",
        r"\bthe truth (is|about)\b", r"\bnever (do|buy|eat|say)\b",
    ]
]

CONTENT_WORD_MIN_LEN = 5  # crude info-density proxy


def deterministic_score(text: str, words: list[dict], clip_start: float,
                        clip_end: float, duration: float) -> dict:
    """
    Heuristic quality signals for a candidate clip (0-100 scale parts).

    Returns {"total": 0-100, "parts": {...}}. Cheap, no network.
    """
    parts: dict[str, float] = {}
    low = text.lower()

    # Hook patterns (semantic-flavoured regex list, capped).
    hits = sum(1 for rx in HOOK_PATTERN_RES if rx.search(low))
    parts["hook_pattern"] = min(20.0, hits * 10.0)

    # Question / exclamation energy.
    parts["punctuation"] = 0.0
    if QUESTION_RE.search(text):
        parts["punctuation"] += 8.0
    if EXCLAIM_RE.search(text):
        parts["punctuation"] += 7.0

    # Opening pace: words in first 5s of the clip.
    opening = [w for w in words if clip_start <= float(w.get("start", 0.0)) < clip_start + 5.0]
    if opening:
        span = max(0.5, float(opening[-1].get("end", clip_start)) - float(opening[0].get("start", clip_start)))
        wpm = len(opening) / (span / 60.0)
        parts["opening_pace"] = 15.0 if wpm >= 130 else (wpm / 130.0 * 15.0)
        if wpm < 80:
            parts["opening_pace"] -= 5.0
    else:
        parts["opening_pace"] = -5.0

    # Dead-air opening: first word starts late.
    first = min((float(w.get("start", 1e9)) for w in words
                 if float(w.get("start", 1e9)) >= clip_start - 0.5), default=None)
    parts["dead_air_opening"] = -12.0 if (first is not None and first - clip_start > 1.5) else 0.0

    # Filler density inside clip.
    clip_words = [w for w in words if clip_start <= float(w.get("start", 0.0)) < clip_end]
    if clip_words:
        fill = sum(1 for w in clip_words if _is_filler_token(_norm_token(str(w.get("word", "")))))
        parts["filler_density"] = -min(15.0, fill / max(len(clip_words), 1) * 150.0)
    else:
        parts["filler_density"] = -10.0

    # Info density proxy: share of long content words.
    if clip_words:
        content = sum(1 for w in clip_words if len(_norm_token(str(w.get("word", "")))) >= CONTENT_WORD_MIN_LEN)
        parts["info_density"] = min(15.0, content / max(len(clip_words), 1) * 40.0)
    else:
        parts["info_density"] = 0.0

    # Completeness: ends on sentence terminal — mid-sentence cuts bleed viewers.
    # Increased penalty so a clean ending can outrank a high viral_score with broken payoff.
    parts["clean_ending"] = 12.0 if _SENT_END_RE.search(text.strip()) else -18.0

    # Length sweet spot 15-75s.
    dur = clip_end - clip_start
    if 15 <= dur <= 75:
        parts["length"] = 10.0
    elif 10 <= dur < 15 or 75 < dur <= 100:
        parts["length"] = 5.0
    else:
        parts["length"] = -5.0

    # Hook must be early: if no hook pattern and no question, penalize
    if parts["hook_pattern"] == 0 and parts["punctuation"] == 0:
        parts["hook_missing"] = -8.0
    else:
        parts["hook_missing"] = 0.0

    total = _clamp_num(sum(parts.values()), -20.0, 100.0)
    return {"total": round(total, 1), "parts": {k: round(v, 1) for k, v in parts.items()}}


def _clamp_num(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


# ---------------------------------------------------------------------------
# Merge / dedupe / rank (section 13)
# ---------------------------------------------------------------------------

def _iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    dur_a = max(0.0, a_end - a_start)
    dur_b = max(0.0, b_end - b_start)
    union = max(1e-6, dur_a + dur_b - inter)
    return inter / union


def combined_score(suggestion: dict) -> float:
    """
    Blend LLM semantic scores (0-10 each) with deterministic total (0-100).

    Hook/payoff weighted higher than generic viral_score; deterministic
    clean-ending penalty now large enough to demote mid-sentence cuts.
    """
    llm = (
        float(suggestion.get("hook", 5)) * 2.2
        + float(suggestion.get("payoff", 5)) * 1.8
        + float(suggestion.get("viral_score", 5)) * 1.2
        + float(suggestion.get("curiosity", 5)) * 0.7
        + float(suggestion.get("emotion", 5)) * 0.5
        + float(suggestion.get("info", 5)) * 0.5
        + float(suggestion.get("narrative", 5)) * 0.5
    )
    llm = _clamp_num(llm, 0.0, 60.0)
    det = float((suggestion.get("_det") or {}).get("total", 50.0))
    det = _clamp_num(det, 0.0, 100.0) * 0.4
    return round(llm + det, 1)


def select_clips(candidates: list[dict], top_k: int = TOP_CLIPS) -> list[dict]:
    """
    Merge heavy overlaps (keep higher combined score), drop near-duplicates,
    rank by combined score, return top_k sorted by start time for stable UX.
    """
    scored = []
    for c in candidates:
        c = dict(c)
        c["_combined"] = combined_score(c)
        scored.append(c)
    scored.sort(key=lambda c: c["_combined"], reverse=True)

    chosen: list[dict] = []
    for c in scored:
        dup = False
        for kept in chosen:
            if _iou(c["start"], c["end"], kept["start"], kept["end"]) >= MERGE_IOU:
                dup = True
                break
        if not dup:
            chosen.append(c)
        if len(chosen) >= top_k:
            break
    chosen.sort(key=lambda c: c["start"])
    return chosen