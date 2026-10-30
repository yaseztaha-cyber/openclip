"""Deterministic campaign-eligibility tests (no network, no ffmpeg)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.policy import (UserCampaignPolicy, evaluate_affiliation_policy,
                               POLICY_CLEAR, POLICY_EXCLUDED, POLICY_REVIEW)
from automation.eligibility import CampaignEligibilityEngine


def _base(**kw):
    c = {"campaign_id": "c1", "name": "Test campaign",
         "description": "Create fun clips for our launch.",
         "requirements": "Post 1 clip.", "platforms": ["tiktok"],
         "content_sources": ["https://example.com/video"],
         "affiliations": [], "brands": [], "entities": []}
    c.update(kw)
    return c


def test_eligible_campaign():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base())
    assert ev["status"] == "ELIGIBLE", ev


def test_sexual_campaign_rejected():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base(description="Promote our porn site with explicit clips"))
    assert ev["status"] == "REJECTED"
    assert "sexual_or_adult" in ev["reasons"]


def test_gambling_campaign_rejected():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base(description="Sports betting promotion, casino bonus inside"))
    assert ev["status"] == "REJECTED"
    assert "gambling" in ev["reasons"]


def test_probability_lecture_not_gambling():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base(
        description="Statistics lecture: probability, odds ratios in a math class textbook."))
    assert ev["status"] == "ELIGIBLE", ev


def test_explicit_affiliation_excluded():
    policy = UserCampaignPolicy(excluded_brands=["acme"])
    eng = CampaignEligibilityEngine(policy=policy)
    ev = eng.evaluate(_base(brands=["ACME"]))
    assert ev["status"] == "REJECTED"
    assert "affiliation_excluded" in ev["reasons"]


def test_ambiguous_affiliation_needs_review():
    policy = UserCampaignPolicy(excluded_brands=["acme"])
    eng = CampaignEligibilityEngine(policy=policy)
    ev = eng.evaluate(_base())  # no brand metadata at all
    assert ev["status"] == "NEEDS_REVIEW"
    assert "affiliation_unverified" in ev["reasons"]


def test_no_proxies_language_nationality_ignored():
    """Language/nationality/location fields must never drive affiliation outcomes."""
    policy = UserCampaignPolicy(excluded_brands=["acme"])
    eng = CampaignEligibilityEngine(policy=policy)
    c = _base(brands=["other"], language="he", country="IL",
              creator_nationality="X", location="Tel Aviv")
    out = evaluate_affiliation_policy(c, policy)
    assert out["status"] == POLICY_CLEAR, out
    ev = eng.evaluate(c)
    assert "affiliation_excluded" not in ev["reasons"]


def test_mixed_content_rejected_with_reasons():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base(description="Online casino porn bonus wagering platform"))
    assert ev["status"] == "REJECTED"
    assert "gambling" in ev["reasons"] and "sexual_or_adult" in ev["reasons"]


def test_insufficient_evidence_needs_review():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base(content_sources=[]))
    assert ev["status"] == "NEEDS_REVIEW"
    assert "insufficient_evidence" in ev["reasons"]


def test_music_required_rejected():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base(requirements="Background music required on every clip"))
    assert ev["status"] == "REJECTED"
    assert "music_required" in ev["reasons"]


def test_rejections_never_silent():
    eng = CampaignEligibilityEngine(policy=UserCampaignPolicy())
    ev = eng.evaluate(_base(description="Online casino bonus"))
    assert ev["status"] == "REJECTED"
    assert ev["reasons"] and ev["evidence"]
