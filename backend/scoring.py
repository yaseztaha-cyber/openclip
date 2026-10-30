"""
scoring.py — Configurable multimodal final score.

Combines five signal families (clips-studio inspired):
  text      — hook/curiosity/info/narrative/payoff/viral (LLM + editorial)
  audio     — loudness/burst/silence→speech payoff
  visual    — motion/scene-cut
  reaction  — fused audio+face+motion spikes
  pacing    — dead-air, filler, slow-opening penalties

Each candidate receives per-family subscores (0-10), normalized per-video
where applicable, then weighted into a final 0-100 score.

Weights are configurable via SCORING_WEIGHTS and can be overridden per-call.
No single signal dominates: max family contribution is capped.
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Default weights (sum to ~1.0; tunable via config/settings.yaml later).
# Text dominates because transcript carries most viral meaning, but audio/visual
# together are 35-40% so a screaming payoff isn't missed.
# P5: hook_first_3s is blended into text (30% of text score) — a weak first 3s
# cannot be saved by a good title.
SCORING_WEIGHTS = {
    "text": 0.44,
    "hook_first_3s": 0.12,
    "audio": 0.16,
    "visual": 0.08,
    "reaction": 0.10,
    "pacing": 0.10,  # negative contribution handled separately
}

# Caps: no family can contribute more than this to the final score (prevents domination)
FAMILY_CAPS = {
    "text": 55.0,
    "audio": 20.0,
    "visual": 12.0,
    "reaction": 15.0,
}

# Penalties subtracted after weighted sum (0-15 each)
PACING_PENALTIES = {
    "dead_air_opening": 8.0,
    "silence_inside": 6.0,
    "filler_density": 6.0,
    "slow_opening": 5.0,
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def score_audio_candidate(start: float, end: float,
                          audio_loud: list[float],
                          audio_burst: list[float],
                          audio_silence: list[float]) -> dict:
    """
    Score audio excitement for a clip window [start, end).

    Components (0-10 each):
      loudness_peak — max loudness inside clip
      burst — max burst inside clip (sudden energy jump)
      silence_to_speech — silence→loud pattern at start (hook surprise)
    """
    def _slice(lst: list[float]) -> list[float]:
        s, e = max(0, int(start)), min(len(lst), math.ceil(end))
        return lst[s:e] if e > s else []

    loud = _slice(audio_loud)
    burst = _slice(audio_burst)
    sil = _slice(audio_silence)

    loud_peak = max(loud) * 10.0 if loud else 5.0
    burst_peak = max(burst) * 10.0 if burst else 0.0

    # silence→speech payoff: first 2s silent then loud
    payoff = 0.0
    if sil and loud:
        first2_sil = sum(sil[:2]) / min(2, len(sil)) if len(sil) >= 1 else 0
        first2_loud = sum(loud[:2]) / min(2, len(loud)) if len(loud) >= 1 else 0
        if first2_sil > 0.5 and first2_loud > 0.6:
            payoff = 8.0
        elif any(burst[:3]) and first2_loud > 0.5:
            payoff = 5.0

    # Silence inside penalty (dead air)
    silence_ratio = sum(1 for v in sil if v > 0.5) / max(len(sil), 1) if sil else 0
    silence_penalty = min(10.0, silence_ratio * 20.0)

    # Escalation: energy should BUILD toward the payoff — clips that escalate
    # retain better than ones that start loud and fizzle. Compare max loudness
    # in the last third vs the first third of the window.
    escalation = 5.0
    if loud and len(loud) >= 3:
        third = max(1, len(loud) // 3)
        first_max = max(loud[:third]) if loud[:third] else 0
        last_max = max(loud[-third:]) if loud[-third:] else 0
        if last_max > first_max + 0.15:
            escalation = 9.0
        elif last_max >= first_max - 0.05:
            escalation = 6.5
        else:
            escalation = 3.0  # fizzles out before the payoff

    total = _clamp((loud_peak * 0.35 + burst_peak * 0.35 + payoff * 0.15 + escalation * 0.15) - silence_penalty * 0.3, 0.0, 10.0)
    return {
        "loudness_peak": round(loud_peak, 1),
        "burst": round(burst_peak, 1),
        "silence_to_speech": round(payoff, 1),
        "escalation": round(escalation, 1),
        "silence_penalty": round(silence_penalty, 1),
        "total": round(total, 1),
    }


def score_visual_candidate(start: float, end: float,
                           motion: list[float],
                           scene_cuts: list[float],
                           face_presence: list[float]) -> dict:
    """
    Visual activity for a clip window.

    Components:
      motion — mean motion inside clip
      scene_change — does clip contain a scene cut (dynamic)
      face_visible — face present ratio
    """
    def _slice(lst: list[float]) -> list[float]:
        s, e = max(0, int(start)), min(len(lst), math.ceil(end))
        return lst[s:e] if e > s else []

    mot = _slice(motion)
    sc = _slice(scene_cuts)
    fp = _slice(face_presence)

    motion_score = (sum(mot) / len(mot) * 10.0) if mot else 5.0
    scene_score = 7.0 if any(v > 0.5 for v in sc) else 3.0
    face_score = (sum(fp) / len(fp) * 10.0) if fp else 0.0

    total = _clamp(motion_score * 0.3 + scene_score * 0.2 + face_score * 0.5, 0.0, 10.0)
    return {
        "motion": round(motion_score, 1),
        "scene_change": round(scene_score, 1),
        "face_visible": round(face_score, 1),
        "total": round(total, 1),
    }


def score_reaction_candidate(start: float, end: float,
                             reaction: list[float],
                             face_delta: list[float]) -> dict:
    """
    Reaction signal: fused audio+face+motion spikes.
    Higher means surprise/laughter/excitement visible.
    """
    def _slice(lst: list[float]) -> list[float]:
        s, e = max(0, int(start)), min(len(lst), math.ceil(end))
        return lst[s:e] if e > s else []

    reac = _slice(reaction)
    fd = _slice(face_delta)

    reac_peak = max(reac) * 10.0 if reac else 0.0
    reac_mean = (sum(reac) / len(reac) * 10.0) if reac else 0.0
    fd_peak = max(fd) * 10.0 if fd else 0.0

    total = _clamp(reac_peak * 0.5 + reac_mean * 0.3 + fd_peak * 0.2, 0.0, 10.0)
    return {
        "reaction_peak": round(reac_peak, 1),
        "reaction_mean": round(reac_mean, 1),
        "face_delta_peak": round(fd_peak, 1),
        "total": round(total, 1),
    }


def score_pacing_candidate(start: float, end: float,
                           silence_mask: list[float],
                           filler_density: float,
                           opening_wpm: Optional[float] = None) -> dict:
    """
    Pacing penalties (0-10, higher = worse). Returned as penalties to subtract.

    filler_density: 0-1 ratio already computed elsewhere.
    opening_wpm: words per minute in first 5s (if available).
    """
    def _slice(lst: list[float]) -> list[float]:
        s, e = max(0, int(start)), min(len(lst), math.ceil(end))
        return lst[s:e] if e > s else []

    sil = _slice(silence_mask)
    dead_ratio = sum(1 for v in sil if v > 0.5) / max(len(sil), 1) if sil else 0
    dead_penalty = _clamp(dead_ratio * 25.0, 0.0, 10.0)

    filler_penalty = _clamp(filler_density * 30.0, 0.0, 10.0)

    slow_penalty = 0.0
    if opening_wpm is not None:
        if opening_wpm < 90:
            slow_penalty = _clamp((90 - opening_wpm) / 40.0 * 10.0, 0.0, 10.0)

    total = _clamp(dead_penalty * 0.5 + filler_penalty * 0.3 + slow_penalty * 0.2, 0.0, 10.0)
    return {
        "dead_air": round(dead_penalty, 1),
        "filler": round(filler_penalty, 1),
        "slow_opening": round(slow_penalty, 1),
        "total": round(total, 1),
    }


def score_hook_first_3s(candidate: dict, sentences: list[dict], words: list[dict]) -> dict:
    """
    Score the first 3 seconds of a candidate's opening (P5).
    Uses the first sentence's hook pattern + question + curiosity.
    Returns {score 0-10, reasons}.
    """
    start = float(candidate.get("start", 0.0))
    # Find first sentence starting at or near candidate start
    first_sent = None
    for s in sentences:
        if abs(s["start"] - start) < 0.5:
            first_sent = s
            break
    if first_sent is None:
        # Fallback: first sentence overlapping start
        for s in sentences:
            if s["start"] <= start + 0.3 and s["end"] >= start - 0.3:
                first_sent = s
                break
    if first_sent is None:
        return {"score": 5.0, "reasons": ["no_sentence"]}

    txt = first_sent.get("text", "").strip()
    low = txt.lower()
    score = 5.0
    reasons: list[str] = []
    # Hook patterns: claim / gap / turn / stakes (not just classic clickbait words)
    hook_words = ["nobody", "mistake", "wrong", "problem", "reason", "never", "stop", "truth",
                  "what if", "turns out", "here's", "but then", "secret", "warning", "discover",
                  "changed everything", "biggest", "worst", "best ", "only way", "first time",
                  "i spent", "i tried", "i learned", "i was wrong", "used to", "instead",
                  "however", "versus", "compared to", "million", "billion", "years",
                  "truth about", "don't ever", "stop doing", "most people"]
    if any(w in low for w in hook_words) or "?" in txt:
        score += 3.0
        reasons.append("hook_pattern")
    # Concrete number/stat in the opening = instant credibility gap
    import re as _re_hook
    if _re_hook.search(r"\d+", txt):
        score += 1.5
        reasons.append("number_stat")
    # Direct address pulls the scroller in
    if _re_hook.search(r"\byou\b|\byour\b", low):
        score += 1.0
        reasons.append("direct_address")
    # Generic opening penalty
    if low.startswith(("so ", "well ", "okay", "right ", "now ", "hey ", "hi ", "hello", "um ", "uh ")):
        score -= 3.5
        reasons.append("generic_opening")
    # Length: too short (<5 words) or too long (>20) for hook
    word_count = len(txt.split())
    if 6 <= word_count <= 14:
        score += 1.0
    elif word_count < 5:
        score -= 1.5
        reasons.append("too_short")
    # Title mismatch: if title contains strong hook but first sentence doesn't, penalize
    title = str(candidate.get("title", "")).lower()
    if any(w in title for w in hook_words) and not any(w in low for w in hook_words):
        score -= 2.0
        reasons.append("title_mismatch")

    return {"score": max(0.0, min(10.0, score)), "reasons": reasons}


def final_multimodal_score(candidate: dict,
                           audio_scores: Optional[dict] = None,
                           visual_scores: Optional[dict] = None,
                           reaction_scores: Optional[dict] = None,
                           pacing_scores: Optional[dict] = None,
                           weights: Optional[dict[str, float]] = None,
                           hook_first_3s: Optional[dict] = None,
                           quality_scores: Optional[dict] = None) -> dict:
    """
    Combine all signal families into a final 0-100 score.

    candidate: dict with hook/curiosity/info/narrative/payoff/viral_score and _det
    audio_scores/visual_scores/reaction_scores/pacing_scores: dicts with "total" 0-10

    Returns {final: 0-100, breakdown: {family: contribution}, penalties}
    """
    w = {**SCORING_WEIGHTS, **(weights or {})}
    # Normalize weights to sum 1.0 for non-pacing families
    text_w = w.get("text", 0.52)
    audio_w = w.get("audio", 0.18)
    visual_w = w.get("visual", 0.09)
    reaction_w = w.get("reaction", 0.11)

    # Text score: reuse editorial.combined_score logic but extract text part
    # If candidate already has _det, use it; else compute from LLM scores alone
    try:
        import editorial
        text_total = editorial.combined_score(candidate)  # 0-100 already weighted 60+40
        # Map to 0-10 for uniform weighting
        text_10 = _clamp(text_total / 10.0, 0.0, 10.0)
    except Exception:
        # Fallback: average LLM scores
        keys = ["hook", "payoff", "curiosity", "emotion", "info", "narrative", "viral_score"]
        vals = [float(candidate.get(k, 5)) for k in keys]
        text_10 = sum(vals) / len(vals)

    # P5: Blend hook_first_3s into text (30% hook, 70% text) — title must match actual opening
    hook_10 = None
    if hook_first_3s is not None:
        hook_10 = float(hook_first_3s.get("score", 5.0))
        text_10 = _clamp(text_10 * 0.7 + hook_10 * 0.3, 0.0, 10.0)

    audio_10 = float((audio_scores or {}).get("total", 5.0))
    visual_10 = float((visual_scores or {}).get("total", 5.0))
    reaction_10 = float((reaction_scores or {}).get("total", 5.0))
    pacing_10 = float((pacing_scores or {}).get("total", 0.0))

    # Guard: loud noise without meaningful text (hook <4) should not dominate
    # e.g., mic bump, music spike with weak transcript → halve audio
    if hook_10 is not None and hook_10 < 4.0 and audio_10 > 7.0:
        audio_10 *= 0.5
        logger.debug(f"Guard: loud audio {audio_10*2:.1f} with weak hook {hook_10:.1f} → halved")

    # Alignment: high audio burst must co-occur with speech (pacing not silent)
    # If candidate is mostly silence but burst high → likely artifact, dampen
    if pacing_10 > 7.0 and audio_10 > 6.0:
        audio_10 *= 0.7
        reaction_10 *= 0.7

    # Weighted sum (pacing subtracts)
    raw = (
        text_10 * text_w * 10
        + audio_10 * audio_w * 10
        + visual_10 * visual_w * 10
        + reaction_10 * reaction_w * 10
    )
    # Pacing penalty: subtract up to 10 points
    pacing_weight = w.get("pacing", 0.10)
    penalty = pacing_10 * pacing_weight * 10

    # Family caps
    text_contrib = min(text_10 * text_w * 10, FAMILY_CAPS["text"])
    audio_contrib = min(audio_10 * audio_w * 10, FAMILY_CAPS["audio"])
    visual_contrib = min(visual_10 * visual_w * 10, FAMILY_CAPS["visual"])
    reaction_contrib = min(reaction_10 * reaction_w * 10, FAMILY_CAPS["reaction"])

    # Editorial quality adjustment: hook/payoff/context evals (0-10 each) now
    # MOVE the ranking, not just the review UI. Weak payoff or incomprehensible
    # context drags a clip down; strong all-round craft lifts it. Range ±8.
    quality_adj = 0.0
    quality_raw: dict = {}
    if quality_scores:
        qh = quality_scores.get("hook_eval")
        qp = quality_scores.get("payoff_eval")
        qc = quality_scores.get("context_eval")
        parts, wsum = 0.0, 0.0
        if qh is not None:
            parts += float(qh) * 0.4
            wsum += 0.4
            quality_raw["hook_eval_10"] = round(float(qh), 1)
        if qp is not None:
            parts += float(qp) * 0.35
            wsum += 0.35
            quality_raw["payoff_eval_10"] = round(float(qp), 1)
        if qc is not None:
            parts += float(qc) * 0.25
            wsum += 0.25
            quality_raw["context_eval_10"] = round(float(qc), 1)
        if wsum > 0:
            quality_10 = parts / wsum
            quality_adj = (quality_10 - 5.0) * 1.6

    final = _clamp(text_contrib + audio_contrib + visual_contrib + reaction_contrib - penalty + quality_adj, 0.0, 100.0)

    raw_hook = round(hook_10, 1) if hook_10 is not None else None
    return {
        "final": round(final, 1),
        "breakdown": {
            "text": round(text_contrib, 1),
            "audio": round(audio_contrib, 1),
            "visual": round(visual_contrib, 1),
            "reaction": round(reaction_contrib, 1),
            "pacing_penalty": round(penalty, 1),
            "editorial_quality": round(quality_adj, 1),
        },
        "raw": {
            "text_10": round(text_10, 1),
            "hook_first_3s_10": raw_hook,
            "audio_10": round(audio_10, 1),
            "visual_10": round(visual_10, 1),
            "reaction_10": round(reaction_10, 1),
            "pacing_10": round(pacing_10, 1),
            **quality_raw,
        },
    }
