"""
eligibility.py — CampaignEligibilityEngine.

Rule order (§59):
  1. Platform eligibility
  2. Sexual/adult exclusion
  3. Gambling exclusion
  4. Music restriction (applied at clip level; campaign-level pre-check only
     rejects campaigns whose requirements DEMAND background music)
  5. Configured affiliation exclusion
  6. Content/source availability
  7. Technical feasibility
  8. Campaign quality score (ranking gate — rejected campaigns never ranked)

Every campaign gets ELIGIBLE | REJECTED | NEEDS_REVIEW plus stored reasons.
Rejected campaigns are never silently discarded.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .policy import (
    UserCampaignPolicy, evaluate_affiliation_policy,
    POLICY_CLEAR, POLICY_EXCLUDED, POLICY_REVIEW,
)

logger = logging.getLogger(__name__)

ELIGIBLE = "ELIGIBLE"
REJECTED = "REJECTED"
NEEDS_REVIEW = "NEEDS_REVIEW"

# --- Explicit-evidence keyword sets (documented, conservative) ---

# Strong indicators: documented adult-content campaign language.
SEXUAL_STRONG = [
    "porn", "pornography", "xxx", "adult film", "escort service",
    "sexual services", "explicit nudity", "onlyfans promotion",
]
# Weak indicators: need more context → NEEDS_REVIEW, never auto-reject.
SEXUAL_WEAK = [
    "sexy", "nude", "nsfw", "erotic", "fetish",
]

GAMBLING_STRONG = [
    "sports betting", "betting site", "online casino", "casino bonus",
    "wagering platform", "betting promotion", "slots bonus", "poker for money",
    "gambling", "sportsbook",
]
# Weak: probability/odds talk in an educational context must NOT auto-reject.
GAMBLING_WEAK = [
    "bet ", "bets ", "betting", "casino", "poker", "slots", "odds", "wager",
]

# Campaign explicitly REQUIRES background music → conflicts with no-music rule.
MUSIC_REQUIRED = [
    "background music required", "must include music", "music bed required",
    "add background music", "include soundtrack",
]

# Academic/educational contexts that neutralize weak gambling hits.
EDUCATIONAL_CONTEXT = [
    "probability", "statistics", "academic", "lecture", "textbook",
    "math class", "research paper",
]

EDUCATIONAL_SEXUAL_CONTEXT = [
    "medical", "educational", "health class", "biology", "anatomy lecture",
]


def _blob(campaign: dict) -> str:
    parts = [
        str(campaign.get("name") or ""),
        str(campaign.get("description") or ""),
        str(campaign.get("requirements") or ""),
        str(campaign.get("allowed_content") or ""),
    ]
    reqs = campaign.get("requirement_list") or []
    if isinstance(reqs, list):
        parts.extend(str(r) for r in reqs)
    return "\n".join(parts).lower()


def _find_all(blob: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if p in blob]


def _check_sexual(blob: str) -> tuple[str, list[str], float]:
    """Returns (decision, evidence, confidence)."""
    strong = _find_all(blob, SEXUAL_STRONG)
    if strong:
        # Educational/medical discussion alone must not trigger rejection.
        if any(c in blob for c in EDUCATIONAL_SEXUAL_CONTEXT) and not any(
                s in ("porn", "pornography", "xxx", "escort service", "sexual services") for s in strong):
            return NEEDS_REVIEW, [f"ambiguous adult-language match: {strong} in educational context"], 0.4
        return REJECTED, [f"explicit adult-content language: {s}" for s in strong], 0.9
    weak = _find_all(blob, SEXUAL_WEAK)
    if weak:
        return NEEDS_REVIEW, [f"possible adult-content language needs context: {weak}"], 0.4
    return ELIGIBLE, [], 1.0


def _check_gambling(blob: str) -> tuple[str, list[str], float]:
    strong = _find_all(blob, GAMBLING_STRONG)
    if strong:
        if any(c in blob for c in EDUCATIONAL_CONTEXT):
            return NEEDS_REVIEW, [f"gambling-language match in educational context: {strong}"], 0.45
        return REJECTED, [f"explicit gambling language: {s}" for s in strong], 0.9
    weak = _find_all(blob, GAMBLING_WEAK)
    if weak:
        if any(c in blob for c in EDUCATIONAL_CONTEXT):
            return ELIGIBLE, [], 0.8
        return NEEDS_REVIEW, [f"possible gambling language needs context: {weak}"], 0.4
    return ELIGIBLE, [], 1.0


class CampaignEligibilityEngine:
    """Explicit, auditable campaign eligibility checks."""

    def __init__(self, policy: Optional[UserCampaignPolicy] = None,
                 allowed_platforms: Optional[list] = None):
        self.policy = policy or UserCampaignPolicy()
        self.allowed_platforms = [p.lower() for p in (allowed_platforms or [])]

    def evaluate(self, campaign: dict) -> dict:
        cid = str(campaign.get("campaign_id") or campaign.get("id") or "unknown")
        reasons: list[str] = []
        evidence: list[dict] = []
        rules: list[str] = []
        worst = ELIGIBLE

        def bump(decision: str):
            nonlocal worst
            order = {ELIGIBLE: 0, NEEDS_REVIEW: 1, REJECTED: 2}
            if order[decision] > order[worst]:
                worst = decision

        # 1. Platform eligibility
        plats = [str(p).lower() for p in (campaign.get("platforms") or [])]
        if self.allowed_platforms:
            if not plats:
                bump(NEEDS_REVIEW)
                reasons.append("platform_unspecified")
                evidence.append({"rule": "platform_eligibility", "summary": "campaign declares no platforms"})
                rules.append("platform_eligibility")
            elif not any(p in self.allowed_platforms for p in plats):
                bump(REJECTED)
                reasons.append("platform_not_allowed")
                evidence.append({"rule": "platform_eligibility",
                                 "summary": f"platforms {plats} not in allowed {self.allowed_platforms}"})
                rules.append("platform_eligibility")

        blob = _blob(campaign)

        # 2. Sexual/adult exclusion
        if self.policy.exclude_adult:
            dec, ev, conf = _check_sexual(blob)
            bump(dec)
            if dec != ELIGIBLE:
                reasons.append("sexual_or_adult")
                rules.append("exclude_adult")
                for e in ev:
                    evidence.append({"rule": "sexual_or_adult", "summary": e, "confidence": conf})

        # 3. Gambling exclusion
        if self.policy.exclude_gambling:
            dec, ev, conf = _check_gambling(blob)
            bump(dec)
            if dec != ELIGIBLE:
                reasons.append("gambling")
                rules.append("exclude_gambling")
                for e in ev:
                    evidence.append({"rule": "gambling", "summary": e, "confidence": conf})

        # 4. Music restriction (campaign-level: only when music is REQUIRED)
        if self.policy.reject_background_music:
            hits = _find_all(blob, MUSIC_REQUIRED)
            if hits:
                bump(REJECTED)
                reasons.append("music_required")
                rules.append("no_background_music")
                evidence.append({"rule": "no_background_music",
                                 "summary": f"campaign requirements demand background music: {hits}"})

        # 5. Configured affiliation exclusion (explicit metadata only)
        aff = evaluate_affiliation_policy(campaign, self.policy)
        if aff["status"] == POLICY_EXCLUDED:
            bump(REJECTED)
            reasons.append("affiliation_excluded")
            rules.append("user_affiliation_exclusion")
            evidence.extend(aff["evidence"])
        elif aff["status"] == POLICY_REVIEW:
            bump(NEEDS_REVIEW if self.policy.ambiguous_action == "NEEDS_REVIEW" else REJECTED)
            reasons.append("affiliation_unverified")
            rules.append("user_affiliation_exclusion")
            evidence.extend(aff["evidence"])

        # 6. Content/source availability — missing sources = insufficient evidence
        sources = campaign.get("content_sources") or []
        if not sources:
            bump(NEEDS_REVIEW)
            reasons.append("insufficient_evidence")
            rules.append("content_availability")
            evidence.append({"rule": "content_availability",
                             "summary": "campaign provides no content sources; cannot verify eligibility"})

        # 7. Technical feasibility (declared constraints we cannot meet are REVIEW, not reject)
        if campaign.get("requirements") and len(str(campaign["requirements"])) > 5000:
            bump(NEEDS_REVIEW)
            reasons.append("requirements_unparseable")
            rules.append("technical_feasibility")
            evidence.append({"rule": "technical_feasibility",
                             "summary": "requirements text unusually long; needs human check"})

        # Ambiguous-action override: owner may escalate REVIEW → REJECT
        if worst == NEEDS_REVIEW and self.policy.ambiguous_action == "REJECT":
            worst = REJECTED

        return {
            "campaign_id": cid,
            "status": worst,
            "reasons": reasons,
            "rules_triggered": sorted(set(rules)),
            "evidence": evidence,
        }
