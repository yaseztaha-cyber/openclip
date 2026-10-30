"""
policy.py — User-configured campaign policy (transparent exclusion rules).

The owner configures explicit exclusions. The system matches campaigns ONLY
against:

  - excluded_affiliations  (explicit affiliation strings the owner lists)
  - excluded_campaigns      (explicit campaign ids)
  - excluded_brands         (explicit brand names)
  - excluded_entities       (explicit entity names)

...compared against EXPLICIT campaign metadata fields:

  - campaign["affiliations"]  (list of stated affiliations)
  - campaign["brands"]        (list of brand names in metadata)
  - campaign["entities"]      (list of named entities in metadata)
  - campaign["id"]

The system MUST NOT infer affiliation from nationality, ethnicity, language,
location, religion, personal identity, or unrelated political discussion.
There is intentionally NO code path in this module that reads country /
language / nationality / religion signals. Anything without sufficient
explicit evidence becomes POLICY_REVIEW (NEEDS_REVIEW), never a silent
classification.

Output states: POLICY_CLEAR | POLICY_EXCLUDED | POLICY_REVIEW
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)

POLICY_CLEAR = "POLICY_CLEAR"
POLICY_EXCLUDED = "POLICY_EXCLUDED"
POLICY_REVIEW = "POLICY_REVIEW"

POLICY_KEY = "automation_user_policy"

DEFAULT_POLICY: dict = {
    "exclude_adult": True,
    "exclude_gambling": True,
    "reject_background_music": True,
    "apply_affiliation_exclusions": True,
    "ambiguous_action": "NEEDS_REVIEW",  # or "REJECT"
    "blur_uncertain_people": False,
    "excluded_affiliations": [],
    "excluded_campaigns": [],
    "excluded_brands": [],
    "excluded_entities": [],
}


@dataclass
class UserCampaignPolicy:
    exclude_adult: bool = True
    exclude_gambling: bool = True
    reject_background_music: bool = True
    apply_affiliation_exclusions: bool = True
    ambiguous_action: str = "NEEDS_REVIEW"
    blur_uncertain_people: bool = False
    excluded_affiliations: list = field(default_factory=list)
    excluded_campaigns: list = field(default_factory=list)
    excluded_brands: list = field(default_factory=list)
    excluded_entities: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "UserCampaignPolicy":
        base = dict(DEFAULT_POLICY)
        if data:
            for k in base:
                if k in data:
                    base[k] = data[k]
        return cls(**base)

    def to_dict(self) -> dict:
        return asdict(self)


def canonical_hash(payload: dict) -> str:
    """Deterministic hash of policy content (key order independent)."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _norm(values) -> set[str]:
    out: set[str] = set()
    if not values:
        return out
    for v in values:
        if v is None:
            continue
        s = str(v).strip().lower()
        if s:
            out.add(s)
    return out


def evaluate_affiliation_policy(campaign: dict, policy: UserCampaignPolicy) -> dict:
    """
    Evaluate the user-configured affiliation exclusion against EXPLICIT
    campaign metadata only.

    Returns {"policy": "user_affiliation_exclusion", "status": ..., "evidence": [...]}.
    """
    if not policy.apply_affiliation_exclusions:
        return {"policy": "user_affiliation_exclusion", "status": POLICY_CLEAR, "evidence": []}

    meta_aff = _norm(campaign.get("affiliations"))
    meta_brands = _norm(campaign.get("brands"))
    meta_entities = _norm(campaign.get("entities"))
    meta_id = str(campaign.get("campaign_id") or campaign.get("id") or "").strip().lower()

    evidence: list[dict] = []

    for excl in _norm(policy.excluded_campaigns):
        if meta_id and meta_id == excl:
            evidence.append({"type": "campaign_id", "source": "user_policy.excluded_campaigns",
                             "summary": f"campaign id '{meta_id}' is explicitly excluded"})
    for excl in _norm(policy.excluded_affiliations):
        if excl in meta_aff:
            evidence.append({"type": "campaign_metadata", "source": "campaign.affiliations",
                             "summary": f"campaign explicitly states affiliation '{excl}'"})
    for excl in _norm(policy.excluded_brands):
        if excl in meta_brands:
            evidence.append({"type": "campaign_metadata", "source": "campaign.brands",
                             "summary": f"campaign metadata names excluded brand '{excl}'"})
    for excl in _norm(policy.excluded_entities):
        if excl in meta_entities:
            evidence.append({"type": "campaign_metadata", "source": "campaign.entities",
                             "summary": f"campaign metadata names excluded entity '{excl}'"})

    if evidence:
        return {"policy": "user_affiliation_exclusion", "status": POLICY_EXCLUDED, "evidence": evidence}

    # No explicit evidence either way. If the campaign carries no affiliation
    # metadata at all AND the owner configured exclusions, we cannot confirm
    # or deny — flag for human review rather than guessing.
    configured = (_norm(policy.excluded_affiliations) | _norm(policy.excluded_brands)
                  | _norm(policy.excluded_entities) | _norm(policy.excluded_campaigns))
    if configured and not (meta_aff or meta_brands or meta_entities):
        return {"policy": "user_affiliation_exclusion", "status": POLICY_REVIEW,
                "evidence": [{"type": "insufficient_evidence",
                              "source": "campaign.metadata",
                              "summary": "no explicit affiliation/brand/entity metadata; cannot confirm exclusion status"}]}
    return {"policy": "user_affiliation_exclusion", "status": POLICY_CLEAR, "evidence": []}
