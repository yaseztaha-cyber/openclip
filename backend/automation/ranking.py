"""
ranking.py — Campaign ranking for ELIGIBLE campaigns only.

Rejected / needs-review campaigns are never scored as opportunities.
  campaign_score = content availability + clip potential + quality
                   + audience fit + workflow feasibility
"""

from __future__ import annotations

WEIGHTS = {
    "content_availability": 0.25,
    "clip_potential": 0.25,
    "quality": 0.20,
    "audience_fit": 0.15,
    "workflow_feasibility": 0.15,
}


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def score_campaign(campaign: dict, signals: dict | None = None) -> dict:
    """
    signals (optional, 0-100 each): content_availability, clip_potential,
    quality, audience_fit, workload (higher workload lowers feasibility).
    Missing signals fall back to transparent metadata-derived estimates.
    """
    signals = signals or {}
    sources = campaign.get("content_sources") or []
    content_availability = signals.get(
        "content_availability", 80.0 if len(sources) >= 2 else (55.0 if sources else 20.0))
    clip_potential = float(signals.get("clip_potential", 60.0))
    quality = float(signals.get("quality", 60.0))
    audience_fit = float(signals.get("audience_fit", 60.0))
    workload = float(signals.get("estimated_workload", 40.0))  # 0-100, higher = harder
    feasibility = _clamp(100.0 - workload)

    parts = {
        "content_availability": _clamp(content_availability),
        "clip_potential": _clamp(clip_potential),
        "quality": _clamp(quality),
        "audience_fit": _clamp(audience_fit),
        "workflow_feasibility": feasibility,
    }
    total = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)
    return {
        "campaign_id": str(campaign.get("campaign_id") or campaign.get("id")),
        "score": round(total, 1),
        "parts": {k: round(v, 1) for k, v in parts.items()},
        "estimated_workload": round(workload, 1),
    }


def rank_campaigns(campaigns: list[dict], evaluations: dict,
                   signals_map: dict | None = None) -> list[dict]:
    """Rank ELIGIBLE campaigns only; anything else is excluded from ranking."""
    signals_map = signals_map or {}
    ranked = []
    for c in campaigns:
        cid = str(c.get("campaign_id") or c.get("id"))
        ev = evaluations.get(cid, {})
        if ev.get("status") != "ELIGIBLE":
            continue
        ranked.append({**score_campaign(c, signals_map.get(cid)), "name": c.get("name")})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked
