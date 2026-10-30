"""
boundaries.py — Start/end optimizers for clip boundaries.

For each LLM-proposed candidate, generate several plausible start/end
variants, score each with hook/end/pacing signals, and pick the best.
This is the "editorial decision" layer that fixes mid-sentence cuts and
payoff truncation without re-asking the LLM.

All candidates are derived from the transcript's sentence segmentation
(and word timings when available), so no hallucinated timestamps.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_SENT_END_RE = re.compile(r"[.?!]+[\"')\]]*\s*$")


def generate_start_candidates(candidate: dict,
                              sentences: list[dict],
                              words: list[dict],
                              max_variants: int = 4) -> list[dict]:
    """
    Generate start variants for a candidate.

    Strategies:
      1. Original LLM start
      2. Sentence start of the hook sentence (snap back)
      3. One sentence before hook (short setup)
      4. Next sentence start (if original had leading filler/greeting)

    Returns list of {start, reason} sorted by start.
    """
    orig = float(candidate["start"])
    cands: list[dict] = [{"start": orig, "reason": "original"}]

    if not sentences:
        return cands

    # Find sentences near orig
    hook_sent = None
    for s in sentences:
        if s["start"] <= orig + 0.3 and s["end"] >= orig - 0.3:
            hook_sent = s
            break
    if hook_sent is None:
        # Nearest sentence start at or before orig
        prev = [s for s in sentences if s["start"] <= orig + 0.05]
        if prev:
            hook_sent = max(prev, key=lambda s: s["start"])

    if hook_sent:
        # Variant: hook sentence start
        if abs(hook_sent["start"] - orig) > 0.15:
            cands.append({"start": float(hook_sent["start"]), "reason": "hook_sentence_start"})

        # Variant: one sentence before (setup)
        idx = next((i for i, s in enumerate(sentences) if s["start"] == hook_sent["start"]), None)
        if idx is not None and idx > 0:
            prev_sent = sentences[idx - 1]
            # Only if setup is short (<18 words) and not too far back
            setup_words = len(prev_sent.get("text", "").split())
            if setup_words <= 18 and (orig - prev_sent["start"]) <= 12.0:
                cands.append({"start": float(prev_sent["start"]), "reason": "setup_sentence"})

        # Variant: next sentence (if original had filler/greeting, skip it)
        if idx is not None and idx + 1 < len(sentences):
            # Check if original sentence is weak (greeting/filler)
            weak_start = re.match(r"^\s*(so|well|okay?|right|now|hey|hi|hello)\b",
                                  hook_sent.get("text", ""), re.IGNORECASE)
            if weak_start:
                nxt = sentences[idx + 1]
                if (nxt["start"] - orig) <= 4.0:
                    cands.append({"start": float(nxt["start"]), "reason": "skip_weak_opener"})

    # Dedupe by start (within 0.15s)
    cands.sort(key=lambda c: c["start"])
    deduped: list[dict] = []
    for c in cands:
        if not deduped or abs(c["start"] - deduped[-1]["start"]) > 0.15:
            deduped.append(c)
    return deduped[:max_variants]


def generate_end_candidates(candidate: dict,
                            sentences: list[dict],
                            max_variants: int = 4) -> list[dict]:
    """
    Generate end variants.

    Strategies:
      1. Original LLM end
      2. Next sentence terminal (include payoff + reaction beat)
      3. +1 sentence beyond that (short resolution)
      4. Snap to sentence end (if original was mid-sentence)
    """
    orig = float(candidate["end"])
    cands: list[dict] = [{"end": orig, "reason": "original"}]

    if not sentences:
        return cands

    # Find sentence containing orig
    cur_sent = None
    for s in sentences:
        if s["start"] <= orig + 0.3 and s["end"] >= orig - 0.3:
            cur_sent = s
            break
    if cur_sent is None:
        prev = [s for s in sentences if s["end"] <= orig + 0.05]
        if prev:
            cur_sent = max(prev, key=lambda s: s["end"])

    if cur_sent:
        idx = next((i for i, s in enumerate(sentences) if s["end"] == cur_sent["end"]), None)
        if idx is not None:
            # If original was mid-sentence, snap to its terminal
            if not _SENT_END_RE.search(cur_sent.get("text", "").strip()) and idx + 1 < len(sentences):
                # Actually snap forward to next terminal
                nxt = sentences[idx + 1] if idx + 1 < len(sentences) else None
                if nxt and (nxt["end"] - orig) <= 6.0:
                    cands.append({"end": float(nxt["end"]), "reason": "sentence_terminal"})
            elif abs(cur_sent["end"] - orig) > 0.5:
                cands.append({"end": float(cur_sent["end"]), "reason": "sentence_terminal"})

            # Extend by 1-2 sentences for resolution (if within 10s)
            for extra in (1, 2):
                j = idx + extra
                if j < len(sentences):
                    nxt = sentences[j]
                    if (nxt["end"] - orig) <= 10.0 and (nxt["end"] - float(candidate["start"])) <= 100.0:
                        cands.append({"end": float(nxt["end"]), "reason": f"resolution+{extra}"})

    cands.sort(key=lambda c: c["end"])
    deduped: list[dict] = []
    for c in cands:
        if not deduped or abs(c["end"] - deduped[-1]["end"]) > 0.4:
            deduped.append(c)
    return deduped[:max_variants]


def score_start_variant(start: float,
                        sentences: list[dict],
                        words: list[dict],
                        audio_loud: Optional[list[float]] = None) -> dict:
    """
    Score a start variant 0-10 (higher = better opening).

    Signals:
      - Sentence-terminal start (+3)
      - Hook pattern in first sentence (+3)
      - No filler/discourse opener (+2)
      - Not mid-sentence (+2)
      - Audio not silent at start (via loudness)
    """
    score = 5.0
    reasons: list[str] = []

    # Find sentence starting at or near this start
    sent = None
    for s in sentences:
        if abs(s["start"] - start) < 0.4:
            sent = s
            break
    if sent is None:
        # Check if mid-sentence (inside a sentence without terminal)
        for s in sentences:
            if s["start"] < start < s["end"] - 0.2:
                if not _SENT_END_RE.search(s.get("text", "").strip()):
                    score -= 3.0
                    reasons.append("mid_sentence")
                break
    else:
        txt = sent.get("text", "").strip()
        if _SENT_END_RE.search(txt):
            score += 1.0
        else:
            score -= 1.0
        # Hook pattern check (lightweight)
        low = txt.lower()
        hook_words = ["nobody", "mistake", "wrong", "problem", "reason", "never", "stop", "truth",
                      "what if", "turns out", "here's", "but then"]
        if any(w in low for w in hook_words) or "?" in txt:
            score += 2.5
            reasons.append("hook_pattern")
        # Weak opener penalty
        if re.match(r"^\s*(so|well|okay?|right|now|hey|hi|hello|um|uh)\b", txt, re.IGNORECASE):
            score -= 3.5
            reasons.append("weak_opener")

    # Audio dead-air check
    if audio_loud is not None and 0 <= int(start) < len(audio_loud):
        if audio_loud[int(start)] < 0.25:  # normalized loudness
            score -= 1.5
            reasons.append("dead_air")

    return {"score": max(0.0, min(10.0, score)), "reasons": reasons}


def score_end_variant(end: float,
                      sentences: list[dict],
                      duration_hint: float = 0.0) -> dict:
    """
    Score an end variant 0-10 (higher = better ending).

    Signals:
      - Sentence terminal (+4)
      - Not too short / not too long
      - Payoff word near end (reveal/conclusion language)
    """
    score = 5.0
    reasons: list[str] = []

    # Find sentence ending at or near end
    sent = None
    for s in sentences:
        if abs(s["end"] - end) < 0.4:
            sent = s
            break
    if sent and _SENT_END_RE.search(sent.get("text", "").strip()):
        score += 4.0
        reasons.append("sentence_terminal")
    elif sent:
        score -= 4.0
        reasons.append("mid_sentence_end")
    else:
        # End not aligned to sentence — assume mid-sentence
        score -= 2.5
        reasons.append("unaligned")

    # Duration hint (if candidate start known, we can check length)
    # Payoff words near end
    if sent:
        low = sent.get("text", "").lower()
        payoff_words = ["therefore", "so ", "finally", "result", "conclusion", "answer",
                        "turns out", "reveal", "discovered", "changed", "because"]
        if any(w in low for w in payoff_words):
            score += 2.0
            reasons.append("payoff_language")

    return {"score": max(0.0, min(10.0, score)), "reasons": reasons}


def optimize_boundaries(candidate: dict,
                        sentences: list[dict],
                        words: list[dict],
                        audio_loud: Optional[list[float]] = None) -> dict:
    """
    Pick the best start/end combination for a candidate.

    Tries all start×end combos (up to 4×4=16), scores each, picks highest
    sum. Returns updated candidate dict with optimized start/end + metadata.

    Constraints: duration 10-100s, never cross (end > start).
    """
    starts = generate_start_candidates(candidate, sentences, words)
    ends = generate_end_candidates(candidate, sentences)

    best = None
    best_score = -1e9
    best_meta = {}

    orig_start = float(candidate["start"])
    orig_end = float(candidate["end"])

    for s in starts:
        for e in ends:
            ns, ne = float(s["start"]), float(e["end"])
            if ne <= ns + 9.5:
                continue
            if ne - ns > 100.0:
                continue
            # Prefer staying close to original (editorial trust) but allow drift
            start_drift = abs(ns - orig_start)
            end_drift = abs(ne - orig_end)
            drift_penalty = min(2.0, (start_drift + end_drift) * 0.15)

            s_score = score_start_variant(ns, sentences, words, audio_loud)["score"]
            e_score = score_end_variant(ne, sentences)["score"]

            # Duration sweet spot bonus
            dur = ne - ns
            if 20 <= dur <= 60:
                dur_bonus = 1.0
            elif 15 <= dur < 20 or 60 < dur <= 80:
                dur_bonus = 0.5
            else:
                dur_bonus = -0.5

            total = s_score + e_score + dur_bonus - drift_penalty
            if total > best_score:
                best_score = total
                best = (ns, ne)
                best_meta = {
                    "start_reason": s["reason"],
                    "end_reason": e["reason"],
                    "start_score": s_score,
                    "end_score": e_score,
                    "drift_penalty": round(drift_penalty, 2),
                }

    if best is None:
        return dict(candidate)

    out = dict(candidate)
    out["start"], out["end"] = best
    out["_boundary"] = best_meta
    out["_boundary_score"] = round(best_score, 1)
    return out
