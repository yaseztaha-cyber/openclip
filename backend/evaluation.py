"""
evaluation.py — Clip quality evaluation pipeline (Phase 1-8).
For every candidate, persist:
  clip_id/start/end/duration + all signal scores + transcript snippets + failure reasons
Provides: hook-first, payoff, context, variant search, redundancy, diversity, review, calibration.
"""
import re
import math
import logging
from typing import Optional

logger = logging.getLogger(__name__)

FAILURE_REASONS = [
    "WEAK_HOOK", "GENERIC_OPENING", "MISSING_CONTEXT", "EXCESSIVE_SETUP",
    "WEAK_PAYOFF", "INCOMPLETE_THOUGHT", "MID_SENTENCE_END", "LOW_INFORMATION",
    "EXCESSIVE_DEAD_AIR", "REDUNDANT", "DUPLICATE_MOMENT",
    "AUDIO_SPIKE_ONLY", "VISUAL_SPIKE_ONLY", "UNCLEAR_REFERENCE",
]

def _norm_token(w: str) -> str:
    return re.sub(r"[^\w']+", "", str(w)).lower()

def extract_snippets(candidate: dict, sentences: list[dict], words: list[dict]) -> dict:
    """Extract first 3s, first sentence, payoff sentence, final sentence transcripts."""
    start, end = float(candidate["start"]), float(candidate["end"])
    # First 3s words
    first3 = [w for w in words if start <= float(w.get("start", 0)) < start+3.0]
    first_3s = " ".join(w.get("word","") for w in first3)[:300]
    # First sentence overlapping start
    first_sent = next((s for s in sentences if abs(s["start"]-start) < 0.5 or (s["start"] <= start+0.3 and s["end"] >= start)), None)
    if not first_sent:
        first_sent = next((s for s in sentences if s["start"] >= start-0.5 and s["start"] <= start+1), None)
    # Payoff sentence: last sentence before end
    payoff_sent = next((s for s in reversed(sentences) if s["end"] <= end+0.3 and s["start"] >= start), None)
    # Final sentence: sentence ending at end
    final_sent = next((s for s in sentences if abs(s["end"]-end) < 0.5), payoff_sent)
    return {
        "first_3s_transcript": first_3s,
        "first_sentence": (first_sent or {}).get("text","")[:300],
        "payoff_sentence": (payoff_sent or {}).get("text","")[:300],
        "final_sentence": (final_sent or {}).get("text","")[:300],
        "_first_sent": first_sent,
        "_payoff_sent": payoff_sent,
        "_final_sent": final_sent,
    }

def evaluate_hook_first(candidate: dict, snippets: dict, words: list[dict]) -> dict:
    """
    Hook-first evaluation (first 1-3s).
    Returns {hook_clarity, hook_curiosity, hook_emotion, hook_information, hook_context, score, reasons}
    """
    start = float(candidate["start"])
    first3_text = snippets.get("first_3s_transcript","")
    first_sent_text = snippets.get("first_sentence","")
    low = (first3_text or first_sent_text).lower()
    # Use word count in first 3s as proxy for information density
    first3_words = [w for w in words if start <= float(w.get("start",0)) < start+3.0]
    # Hook clarity: is first sentence understandable without context?
    hook_clarity = 5.0
    reasons = []
    if re.match(r"^\s*(so|well|okay?|right|now|hey|hi|hello|um|uh)\b", first_sent_text, re.I):
        hook_clarity -= 3.0
        reasons.append("GENERIC_OPENING")
    if len(first3_text.split()) < 4:
        hook_clarity -= 2.0
        reasons.append("WEAK_HOOK")
    if re.search(r"\b(he|she|they|it|this|that)\b.{0,10}\b(he|she|they|it)\b", low) and len(first3_text.split()) < 8:
        hook_clarity -= 1.5
        reasons.append("UNCLEAR_REFERENCE")
    # Hook curiosity: information gap (claims, turns, stakes — not just clickbait words)
    hook_curiosity = 5.0
    curiosity_words = ["nobody","mistake","wrong","problem","reason","never","secret","warning","discover",
                       "what if","turns out","but then","changed everything","biggest","worst","best ",
                       "only way","first time","i spent","i tried","i learned","used to","instead",
                       "however","versus","compared to","million","billion","truth about","most people"]
    if any(w in low for w in curiosity_words) or "?" in first_sent_text:
        hook_curiosity += 2.5
    if re.search(r"\d+", first_sent_text):
        hook_curiosity += 1.0  # concrete number/stat opens a gap
    if re.search(r"\byou\b|\byour\b", low):
        hook_curiosity += 0.5  # direct address
    hook_curiosity = min(10.0, hook_curiosity)
    # Hook emotion: exclamation, strong language
    hook_emotion = 5.0
    if "!" in first_sent_text or any(w in low for w in ["amazing","terrible","shocking","unbelievable","love","hate"]):
        hook_emotion += 2.0
    # Hook information: word count and content words
    hook_information = min(10.0, len(first3_words) * 1.2 + (2 if any(len(_norm_token(w.get("word","")))>=6 for w in first3_words) else 0))
    # Hook context: does first 3s need setup?
    hook_context = 7.0
    if len(first3_words) < 5:
        hook_context -= 2.0
        reasons.append("MISSING_CONTEXT")
    score = (hook_clarity*0.3 + hook_curiosity*0.3 + hook_emotion*0.15 + hook_information*0.15 + hook_context*0.1)
    score = max(0.0, min(10.0, score))
    return {
        "hook_clarity": round(hook_clarity,1), "hook_curiosity": round(hook_curiosity,1),
        "hook_emotion": round(hook_emotion,1), "hook_information": round(hook_information,1),
        "hook_context": round(hook_context,1), "score": round(score,1), "reasons": reasons
    }

def evaluate_payoff(candidate: dict, snippets: dict) -> dict:
    """Payoff evaluation: completeness and strength."""
    payoff_text = snippets.get("payoff_sentence","")
    final_text = snippets.get("final_sentence","")
    low = (payoff_text or final_text).lower()
    payoff_completeness = 5.0
    payoff_strength = 5.0
    reasons = []
    # Check if payoff sentence ends on terminal
    if re.search(r"[.!?][\"')\]]*\s*$", payoff_text.strip()):
        payoff_completeness += 2.0
    else:
        payoff_completeness -= 2.0
        reasons.append("INCOMPLETE_THOUGHT")
        reasons.append("MID_SENTENCE_END")
    # Payoff strength: does it contain conclusion language?
    payoff_words = ["therefore","so ","finally","result","conclusion","answer","turns out","reveal","discovered","because","real reason"]
    if any(w in low for w in payoff_words):
        payoff_strength += 2.5
    # Weak payoff: very short payoff sentence
    if len(payoff_text.split()) < 5:
        payoff_strength -= 2.0
        reasons.append("WEAK_PAYOFF")
    # If payoff is same as hook (redundant)
    if snippets.get("first_sentence","").strip().lower() == payoff_text.strip().lower() and len(payoff_text.split())>3:
        payoff_strength -= 1.5
        reasons.append("REDUNDANT")
    score = (payoff_completeness*0.5 + payoff_strength*0.5)
    return {
        "payoff_completeness": round(payoff_completeness,1),
        "payoff_strength": round(payoff_strength,1),
        "score": round(max(0,min(10,score)),1),
        "reasons": reasons
    }

def evaluate_context(candidate: dict, snippets: dict, sentences: list[dict]) -> dict:
    """Context sufficiency: is clip understandable without preceding conversation?"""
    start = float(candidate["start"])
    first_sent = snippets.get("_first_sent")
    context_score = 7.0
    reasons = []
    if first_sent:
        txt = first_sent.get("text","")
        # Check for anaphoric references needing context
        if re.match(r"^\s*(so|then|but|and|also)\b", txt, re.I):
            context_score -= 2.0
            reasons.append("MISSING_CONTEXT")
        if re.search(r"\b(as i said|as we discussed|you know|like i said)\b", txt, re.I):
            context_score -= 2.5
            reasons.append("UNCLEAR_REFERENCE")
        # Check if previous sentence would help
        idx = next((i for i,s in enumerate(sentences) if s["start"]==first_sent["start"]), None)
        if idx is not None and idx>0:
            prev = sentences[idx-1]
            # If current starts with pronoun, previous likely needed
            if re.match(r"^\s*(he|she|they|it|this|that)\b", txt, re.I):
                context_score -= 1.5
                reasons.append("MISSING_CONTEXT")
                # Suggest earlier start
                reasons.append("EXCESSIVE_SETUP_CANDIDATE")
    # Excessive setup: if first 3s is all setup and hook is later, penalize
    if len(snippets.get("first_3s_transcript","").split()) > 20:
        context_score -= 1.0
        reasons.append("EXCESSIVE_SETUP")
    return {"context_sufficiency": round(max(0,min(10,context_score)),1), "score": round(max(0,min(10,context_score)),1), "reasons": reasons}

def assign_failure_reasons(candidate: dict, hook_eval: dict, payoff_eval: dict, context_eval: dict, pacing_score: dict = None, audio_score: dict = None, visual_score: dict = None) -> list[str]:
    """Combine all evaluators into failure reasons list."""
    reasons = set()
    reasons.update(hook_eval.get("reasons", []))
    reasons.update(payoff_eval.get("reasons", []))
    reasons.update(context_eval.get("reasons", []))
    # Pacing
    if pacing_score and pacing_score.get("total",0) > 7:
        reasons.add("EXCESSIVE_DEAD_AIR")
    # Audio/visual spike only
    if audio_score and audio_score.get("total",0) > 7 and candidate.get("hook",5) < 4 and candidate.get("payoff",5) < 4:
        reasons.add("AUDIO_SPIKE_ONLY")
    if visual_score and visual_score.get("total",0) > 7 and candidate.get("hook",5) < 4:
        reasons.add("VISUAL_SPIKE_ONLY")
    # Low information
    if candidate.get("information", candidate.get("info",5)) < 4:
        reasons.add("LOW_INFORMATION")
    # Generic opening already in hook
    # Map to required set
    mapping = {
        "WEAK_HOOK": "WEAK_HOOK", "GENERIC_OPENING": "GENERIC_OPENING",
        "MISSING_CONTEXT": "MISSING_CONTEXT", "EXCESSIVE_SETUP": "EXCESSIVE_SETUP",
        "WEAK_PAYOFF": "WEAK_PAYOFF", "INCOMPLETE_THOUGHT": "INCOMPLETE_THOUGHT",
        "MID_SENTENCE_END": "MID_SENTENCE_END", "LOW_INFORMATION": "LOW_INFORMATION",
        "EXCESSIVE_DEAD_AIR": "EXCESSIVE_DEAD_AIR", "REDUNDANT": "REDUNDANT",
        "DUPLICATE_MOMENT": "DUPLICATE_MOMENT", "AUDIO_SPIKE_ONLY": "AUDIO_SPIKE_ONLY",
        "VISUAL_SPIKE_ONLY": "VISUAL_SPIKE_ONLY", "UNCLEAR_REFERENCE": "UNCLEAR_REFERENCE",
    }
    out = [mapping[r] for r in reasons if r in mapping]
    # Ensure at least one if low scores
    if not out and (candidate.get("hook",5) < 4 or candidate.get("payoff",5) < 4):
        if candidate.get("hook",5) < 4:
            out.append("WEAK_HOOK")
        if candidate.get("payoff",5) < 4:
            out.append("WEAK_PAYOFF")
    return out[:4]  # limit

def generate_variants(candidate: dict, sentences: list[dict], words: list[dict]) -> list[dict]:
    """
    Generate 4 boundary variants for top candidates:
    A: exact hook → payoff
    B: one sentence setup → hook → payoff
    C: hook → payoff → short reaction (extend 1 sentence)
    D: shorter hook → payoff (trim first sentence if hook is long)
    """
    import boundaries
    base = dict(candidate)
    variants = []
    # A: exact
    variants.append({**base, "variant": "A_exact", "start": base["start"], "end": base["end"]})
    # Use boundaries helper to generate setup and payoff extensions
    # B: one sentence setup
    try:
        sents = [s for s in sentences if s["start"] <= base["start"]+0.3 and s["start"] >= base["start"]-12]
        # Find hook sentence
        hook_sent = next((s for s in sentences if abs(s["start"]-base["start"])<0.5), None)
        if hook_sent:
            idx = next((i for i,s in enumerate(sentences) if s["start"]==hook_sent["start"]), None)
            if idx is not None and idx>0:
                prev = sentences[idx-1]
                if len(prev.get("text","").split()) <= 18:
                    variants.append({**base, "variant": "B_setup", "start": prev["start"], "end": base["end"]})
    except: pass
    # C: payoff + reaction (extend 1 sentence)
    try:
        payoff_sent = next((s for s in sentences if abs(s["end"]-base["end"])<0.5), None)
        if payoff_sent:
            idx = next((i for i,s in enumerate(sentences) if s["end"]==payoff_sent["end"]), None)
            if idx is not None and idx+1 < len(sentences):
                nxt = sentences[idx+1]
                if nxt["end"] - base["start"] <= 100:
                    variants.append({**base, "variant": "C_reaction", "start": base["start"], "end": nxt["end"]})
    except: pass
    # D: shorter hook (trim first sentence if long)
    try:
        hook_sent = next((s for s in sentences if abs(s["start"]-base["start"])<0.5), None)
        if hook_sent and len(hook_sent.get("text","").split()) > 12:
            # Trim to next sentence start
            idx = next((i for i,s in enumerate(sentences) if s["start"]==hook_sent["start"]), None)
            if idx is not None and idx+1 < len(sentences):
                nxt = sentences[idx+1]
                if abs(nxt["start"] - base["start"]) < 4:
                    variants.append({**base, "variant": "D_shorter", "start": nxt["start"], "end": base["end"]})
    except: pass
    # Dedupe and filter duration 10-100
    filtered = []
    seen = set()
    for v in variants:
        if v["end"] - v["start"] < 10 or v["end"] - v["start"] > 100:
            continue
        key = (round(v["start"],1), round(v["end"],1))
        if key in seen:
            continue
        seen.add(key)
        filtered.append(v)
    # Ensure at least A
    if not filtered:
        filtered = [variants[0]]
    return filtered[:4]

def semantic_similarity(a_text: str, b_text: str) -> float:
    """Simple Jaccard over normalized tokens."""
    a = set(re.sub(r"[^\w ]","",a_text.lower()).split())
    b = set(re.sub(r"[^\w ]","",b_text.lower()).split())
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0

def deduplicate_candidates(candidates: list[dict], threshold: float = 0.75) -> list[dict]:
    """Keep strongest per semantic duplicate cluster."""
    # Sort by final_score desc
    sorted_cands = sorted(candidates, key=lambda c: c.get("final_score", c.get("_combined",0)), reverse=True)
    kept = []
    for cand in sorted_cands:
        dup = False
        for k in kept:
            sim = semantic_similarity(cand.get("title","") + " " + cand.get("reason",""),
                                      k.get("title","") + " " + k.get("reason",""))
            # Also check transcript snippets if available
            if cand.get("first_sentence") and k.get("first_sentence"):
                sim = max(sim, semantic_similarity(cand["first_sentence"], k["first_sentence"]))
            if sim >= threshold:
                dup = True
                # Mark failure reason on weaker
                if "failure_reasons" not in cand:
                    cand["failure_reasons"] = []
                if "DUPLICATE_MOMENT" not in cand["failure_reasons"]:
                    cand["failure_reasons"].append("DUPLICATE_MOMENT")
                break
        if not dup:
            kept.append(cand)
    kept.sort(key=lambda c: c.get("start",0))
    return kept

def diversify_selection(candidates: list[dict], top_k: int = 5) -> list[dict]:
    """
    Greedy diversity: pick top, then prefer candidates whose title/ reason is dissimilar to already picked.
    Only when quality comparable (within 10% score).
    """
    if len(candidates) <= top_k:
        return candidates
    # Sort by final_score
    sorted_cands = sorted(candidates, key=lambda c: c.get("final_score",0), reverse=True)
    selected = [sorted_cands[0]]
    remaining = sorted_cands[1:]
    while len(selected) < top_k and remaining:
        # Find candidate with max score/diversity tradeoff
        best = None
        best_score = -1
        for cand in remaining:
            base_score = cand.get("final_score",0)
            # Diversity penalty: max similarity to already selected
            max_sim = max(semantic_similarity(cand.get("title",""), s.get("title","")) for s in selected)
            # Only apply diversity if within 10% of top score
            top_score = selected[0].get("final_score",0)
            if base_score >= top_score * 0.9:
                # Boost diverse
                adjusted = base_score * (1 - max_sim * 0.3)
            else:
                adjusted = base_score
            if adjusted > best_score:
                best_score = adjusted
                best = cand
        if best:
            selected.append(best)
            remaining.remove(best)
        else:
            break
    selected.sort(key=lambda c: c.get("start",0))
    return selected
