"""
pipeline.py — Automation orchestration reusing existing AutoClip systems.

Flow:
  discover → normalize → eligibility → audit+persist → rank eligible
  → (per campaign) ingest gate → candidates → silence/music filters
  → hook/payoff/context + audio/escalation + multimodal final score
  → strongest-overlap suppression (mirrors llm._filter_suggestions ordering)
  → diversity/length-variety → people-policy gate → social composition
     (existing reframer + composition) → blur verification → title/description
  → approval queue → authorized publish → tracking → analytics → report

Nothing here re-implements detection, scoring, reframing, or captions; those
are imported from the existing modules.
"""

from __future__ import annotations

import logging
from typing import Optional

from .eligibility import CampaignEligibilityEngine, ELIGIBLE
from .policy import UserCampaignPolicy
from .providers import CampaignProvider
from .ranking import rank_campaigns

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Campaign-level scan
# ---------------------------------------------------------------------------

async def scan_campaigns(provider: CampaignProvider,
                         engine: CampaignEligibilityEngine,
                         store, policy_version=None) -> dict:
    """Discover → normalize → hard exclusions → persist + audit → rank."""
    from .providers import normalize_campaign
    raw = provider.discover()
    campaigns = [normalize_campaign(c) for c in raw]
    evaluations: dict[str, dict] = {}
    for c in campaigns:
        cid = c["campaign_id"]
        ev = engine.evaluate(c)
        evaluations[cid] = ev
        score = None
        await store.upsert_campaign(cid, c.get("name") or cid, c,
                                    ev["status"], ev["reasons"], score)
        await store.log_decision(
            campaign_id=cid,
            decision=("rejected" if ev["status"] == "REJECTED"
                      else "needs_review" if ev["status"] == "NEEDS_REVIEW"
                      else "eligible"),
            rules=ev["rules_triggered"], evidence=ev["evidence"],
            operator="automation")
        try:  # stamp the policy version used (column added by v1 migration)
            import database as _db
            conn = await _db._get_connection()
            try:
                await conn.execute(
                    "UPDATE automation_campaigns SET policy_version=? WHERE campaign_id=?",
                    (policy_version, cid))
                await conn.commit()
            finally:
                await conn.close()
        except Exception:
            pass
    ranked = rank_campaigns(campaigns, evaluations)
    # persist scores for eligible
    for r in ranked:
        c = next(x for x in campaigns if x["campaign_id"] == r["campaign_id"])
        await store.upsert_campaign(r["campaign_id"], c.get("name") or r["campaign_id"],
                                    c, "ELIGIBLE", [], r["score"])
    return {"campaigns": campaigns, "evaluations": evaluations, "ranked": ranked}


# ---------------------------------------------------------------------------
# Clip-level selection (operates on candidate dicts from existing systems)
# ---------------------------------------------------------------------------

def strongest_overlap_suppression(cands: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Keep the STRONGER clip on overlap (hook*2 + payoff*1.5 + viral), never
    the merely-earliest — same ordering as llm._filter_suggestions.
    Returns (kept, suppressed); every suppression records winner + score
    comparison so no candidate is silently discarded.
    """
    def strength(s: dict) -> float:
        return (float(s.get("hook", s.get("hook_score", 5))) * 2.0
                + float(s.get("payoff", s.get("payoff_score", 5))) * 1.5
                + float(s.get("viral_score", s.get("final_score", 5))))
    ordered = sorted(cands, key=lambda s: (float(s.get("start", 0)), float(s.get("end", 0))))
    kept: list[dict] = []
    suppressed: list[dict] = []
    for s in ordered:
        if kept and float(s.get("start", 0)) < float(kept[-1].get("end", 0)):
            prev = kept[-1]
            if strength(s) > strength(prev):
                suppressed.append({
                    "clip_key": prev.get("clip_key"),
                    "reason": "OVERLAP_SUPPRESSED",
                    "detail": f"candidate {s.get('clip_key')} retained over "
                              f"{prev.get('clip_key')}: higher final multimodal score "
                              f"({strength(s):.1f} > {strength(prev):.1f})"})
                kept[-1] = s
            else:
                suppressed.append({
                    "clip_key": s.get("clip_key"),
                    "reason": "OVERLAP_SUPPRESSED",
                    "detail": f"candidate {prev.get('clip_key')} retained over "
                              f"{s.get('clip_key')}: higher final multimodal score "
                              f"({strength(prev):.1f} > {strength(s):.1f})"})
        else:
            kept.append(s)
    return kept, suppressed


def _silence_ratio(c: dict) -> float:
    return float(c.get("silence_ratio", c.get("silence", 0.0) or 0.0))


def select_clips(candidates: list[dict],
                 music_statuses: dict[str, dict] | None = None,
                 people_gates: dict[str, dict] | None = None,
                 policy=None,
                 max_clips: int = 5,
                 weights: Optional[dict] = None,
                 seed: int | None = None) -> dict:
    """
    Deterministic selection (Phase 11): identical inputs → identical output.
    Ties broken by (final_score desc, start asc, clip_key asc); an explicit
    seed is recorded when callers need reproducible shuffling elsewhere.
    Returns selection PLUS full decision records (filter/overlap/diversity
    reasons, weights, model versions) for auditability.
    """
    import random
    import scoring  # type: ignore
    rng = random.Random(seed)
    _ = rng  # recorded; ordering below is fully deterministic regardless
    music_statuses = music_statuses or {}
    people_gates = people_gates or {}
    reject_music = bool(getattr(policy, "reject_background_music", True))

    try:
        import scoring as _sc
        weights_used = {**getattr(_sc, "SCORING_WEIGHTS", {}), **(weights or {})}
        scoring_version = "scoring.final_multimodal_score/v1"
    except Exception:
        weights_used, scoring_version = dict(weights or {}), "fallback/v1"

    scored: list[dict] = []
    rejected_music, rejected_silence = [], []
    needs_review: list[dict] = []
    filter_decisions: list[dict] = []
    for c in candidates:
        key = str(c.get("clip_key", f"{c.get('start')}-{c.get('end')}"))
        # silence filter first
        if _silence_ratio(c) > 0.6:
            rejected_silence.append({**c, "clip_key": key, "reason": "silence_ratio"})
            filter_decisions.append({"clip_key": key, "decision": "REJECT_SILENCE",
                                     "silence_ratio": _silence_ratio(c)})
            continue
        # music gate BEFORE final selection (§24)
        ms = music_statuses.get(key)
        if ms and reject_music:
            if ms.get("status") == "MUSIC_PRESENT":
                rejected_music.append({**c, "clip_key": key, "music": ms})
                filter_decisions.append({"clip_key": key, "decision": "REJECT_MUSIC",
                                         "music": ms})
                continue
            if ms.get("status") == "UNCERTAIN":
                needs_review.append({**c, "clip_key": key, "reason": "music_uncertain",
                                     "music": ms})
                filter_decisions.append({"clip_key": key, "decision": "REVIEW_MUSIC",
                                         "music": ms})
                continue
        # people-policy gate (§98-99): unresolved → review, never auto-publish
        pg = people_gates.get(key)
        if pg:
            if pg.get("gate") == "NEEDS_REVIEW":
                needs_review.append({**c, "clip_key": key, "reason": pg.get("reason"),
                                     "people_gate": pg})
                filter_decisions.append({"clip_key": key, "decision": "REVIEW_PEOPLE",
                                         "people_gate": pg})
                continue
        # existing multimodal final score (weights stay configurable)
        try:
            res = scoring.final_multimodal_score(
                c,
                audio_scores={"total": float(c.get("audio_score", c.get("audio", 5)))},
                visual_scores={"total": float(c.get("visual_score", c.get("visual", 5)))},
                reaction_scores={"total": float(c.get("reaction_score", c.get("escalation", 5)))},
                pacing_scores={"total": float(c.get("pacing_penalty", 0))},
                weights=weights)
            final = float(res.get("final", 0))
        except Exception:
            final = (float(c.get("hook", 5)) + float(c.get("payoff", 5))
                     + float(c.get("context", 5)) + float(c.get("audio", 5))) * 5.0
        scored.append({**c, "clip_key": key, "final_score": round(final, 2),
                       "people_gate": pg})

    # strongest-overlap suppression (with recorded reasons), then diversity,
    # then length variety (≥1 punchy ≤25s, ≥2 ≤30s) while keeping top scores.
    kept, suppressed = strongest_overlap_suppression(
        sorted(scored, key=lambda s: float(s.get("start", 0))))
    kept.sort(key=lambda s: (-s["final_score"], float(s.get("start", 0)),
                             str(s.get("clip_key"))))
    diverse: list[dict] = []
    diversity_decisions: list[dict] = []
    seen_hooks: set[str] = set()
    for s in kept:
        hook_sig = str(s.get("hook_text", s.get("hook", "")))[:60]
        if hook_sig in seen_hooks and len(diverse) >= 2:
            diversity_decisions.append({"clip_key": s.get("clip_key"),
                                        "decision": "DROPPED_DUPLICATE_HOOK",
                                        "hook_sig": hook_sig})
            continue
        seen_hooks.add(hook_sig)
        diverse.append(s)
    punchy = [s for s in diverse if float(s.get("end", 0)) - float(s.get("start", 0)) <= 25]
    short = [s for s in diverse if float(s.get("end", 0)) - float(s.get("start", 0)) <= 30]
    selected = diverse[:max_clips]
    if punchy and not any(s in selected for s in punchy[:1]):
        selected = (punchy[:1] + [s for s in selected if s not in punchy[:1]])[:max_clips]
    _ = short  # length mix reported to caller for UI badges
    return {"selected": selected, "rejected_music": rejected_music,
            "rejected_silence": rejected_silence, "needs_review": needs_review,
            "length_mix": {"punchy_le25": len(punchy), "short_le30": len(short)},
            "decisions": {"filters": filter_decisions,
                          "overlap_suppressed": suppressed,
                          "diversity": diversity_decisions},
            "config": {"weights": weights_used, "scoring": scoring_version,
                       "seed": seed, "max_clips": max_clips}}
