"""Phase-21 invariant tests: properties that must hold in every mode."""
import asyncio
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from automation import store
from automation.analytics import aggregate_publications
from automation.pipeline import rank_campaigns
from automation.publishing import (MockPublisher, AuthorizedPlatformPublisher,
                                   PublishBlocked, assert_publish_allowed)
from automation.render_identity import compute_render_hash, content_hash
from automation.scheduler import AutomationScheduler
from automation.side_effects import DryRunBlocked
from automation.statemachine import (CAMPAIGN_TRANSITIONS, CLIP_TRANSITIONS,
                                     SUBMISSION_TRANSITIONS, StateTransitionError,
                                     transition)


def _use_tmp_db(tmp_path):
    database.DB_PATH = str(tmp_path / "inv_test.db")


def _approval(render_hash="rh_abc", ttl="+2h", status="APPROVED", policy_version=1):
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = (now + datetime.timedelta(hours=2)).isoformat() if ttl == "+2h" else (
        (now - datetime.timedelta(hours=1)).isoformat() if ttl == "-1h" else None)
    return {"status": status, "expires_at": exp, "render_hash": render_hash,
            "title_hash": content_hash("t"), "description_hash": content_hash("d"),
            "policy_version": policy_version, "invalidated_reason": None}


def _gate_kwargs(**kw):
    d = {"approval": _approval(), "render_hash": "rh_abc", "title": "t",
         "description": "d", "campaign_status": "ELIGIBLE",
         "current_policy_version": 1, "render_verified": True,
         "idempotency_unique": True, "adapter_enabled": True,
         "credential_present": True, "from_hashes": content_hash}
    d.update(kw)
    return d


def test_rejected_campaign_never_ranked():
    camps = [{"campaign_id": "bad", "name": "Bad"}]
    ranked = rank_campaigns(camps, {"bad": {"status": "REJECTED"}})
    assert ranked == []


def test_rejected_clip_never_approved_transition():
    with pytest.raises(StateTransitionError):
        transition(CLIP_TRANSITIONS, "clip", "REJECTED", "APPROVED")
    with pytest.raises(StateTransitionError):
        transition(CLIP_TRANSITIONS, "clip", "PUBLISHED", "PUBLISHING")


def _live(monkeypatch):
    monkeypatch.setenv("AUTOMATION_MODE", "live")
    monkeypatch.setenv("LIVE_PUBLISHING_ENABLED", "true")


def test_unapproved_clip_never_publishes(monkeypatch):
    _live(monkeypatch)
    with pytest.raises(PublishBlocked) as e:
        assert_publish_allowed(**_gate_kwargs(approval=None))
    assert e.value.code == "no_approval"


def test_expired_approval_never_publishes(monkeypatch):
    _live(monkeypatch)
    with pytest.raises(PublishBlocked) as e:
        assert_publish_allowed(**_gate_kwargs(approval=_approval(ttl="-1h")))
    assert e.value.code == "approval_expired"


def test_changed_render_rejects_old_approval(monkeypatch):
    _live(monkeypatch)
    with pytest.raises(PublishBlocked) as e:
        assert_publish_allowed(**_gate_kwargs(render_hash="rh_CHANGED"))
    assert e.value.code == "render_mismatch"


def test_dry_run_never_calls_external_publisher(monkeypatch):
    monkeypatch.setenv("AUTOMATION_MODE", "dry_run")
    monkeypatch.setenv("LIVE_PUBLISHING_ENABLED", "true")
    monkeypatch.setenv("PUBLISH_TOKEN_TIKTOK", "tok")
    with pytest.raises(DryRunBlocked):
        AuthorizedPlatformPublisher().publish("c.mp4", "t", "d", "tiktok", "k",
                                              approval_id="a1")
    with pytest.raises(PublishBlocked):
        assert_publish_allowed(**_gate_kwargs())
    # mock publisher itself performs no external I/O
    m = MockPublisher()
    out = m.publish("c.mp4", "t", "d", "tiktok", "k")
    assert out["mock"] is True


def test_email_never_sent_without_live(monkeypatch):
    from automation.reports import send_email
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("REPORT_RECIPIENTS", "owner@example.com")
    assert send_email("s", "<p>x</p>", "x", None, allow_external=False) == "EMAIL_DISABLED"


def test_missing_token_never_publishes(monkeypatch):
    monkeypatch.setenv("AUTOMATION_MODE", "live")
    monkeypatch.setenv("LIVE_PUBLISHING_ENABLED", "true")
    monkeypatch.delenv("PUBLISH_TOKEN_TIKTOK", raising=False)
    with pytest.raises(PublishBlocked) as e:
        AuthorizedPlatformPublisher().publish("c.mp4", "t", "d", "tiktok", "k",
                                              approval_id="a1")
    assert e.value.code == "missing_credential"


def test_duplicate_idempotency_single_slot(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    assert asyncio.run(store.claim_publication_slot("c1", "camp", "tiktok", "KEY1")) is True
    assert asyncio.run(store.claim_publication_slot("c1", "camp", "tiktok", "KEY1")) is False
    assert asyncio.run(store.claim_publication_slot("c1", "camp", "tiktok", "KEY2")) is True


def test_missing_analytics_never_become_zero():
    out = aggregate_publications([{"id": "p1", "clip_id": "c1"}], [])
    assert out["total_views"] == "NOT AVAILABLE"
    assert out["confirmed_earnings"] == "NOT AVAILABLE"
    assert out["estimated_earnings"] == "NOT AVAILABLE"


def test_policy_history_immutable(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    from automation.policy import UserCampaignPolicy, canonical_hash
    p1 = UserCampaignPolicy()
    h1 = canonical_hash(p1.to_dict())
    v1 = asyncio.run(store.save_policy_version(p1.to_dict(), h1, "owner"))
    asyncio.run(store.log_decision(campaign_id="c1", decision="eligible",
                                   rules=[], evidence=[], operator="automation"))
    p2 = UserCampaignPolicy(exclude_gambling=False)
    v2 = asyncio.run(store.save_policy_version(p2.to_dict(),
                                               canonical_hash(p2.to_dict()), "owner"))
    assert v2["version"] == v1["version"] + 1
    decs = asyncio.run(store.list_decisions("c1"))
    assert len(decs) == 1  # historical decision untouched
    vers = asyncio.run(store.list_policy_versions())
    assert len(vers) == 2


def test_failed_clip_does_not_abort_others(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    sched = AutomationScheduler(max_retries=0)

    async def ok():
        return "fine"

    async def boom():
        raise RuntimeError("clip failed")

    r1 = asyncio.run(sched.run_step("ja", "render", ok, store=store))
    r2 = asyncio.run(sched.run_step("jb", "render", boom, store=store))
    assert r1["status"] == "completed" and r2["status"] == "failed"
    jobs = {j["job_id"]: j["status"] for j in asyncio.run(store.list_jobs())}
    assert jobs["ja"] == "completed" and jobs["jb"] == "failed"


def test_scheduler_restart_skips_completed():
    done = AutomationScheduler.recover_interrupted(
        [{"job_id": "x", "status": "completed", "kind": "render", "attempt": 1}])
    assert done == []
    pub = AutomationScheduler.recover_interrupted(
        [{"job_id": "p", "status": "running", "kind": "publish_clip",
          "attempt": 1, "heartbeat_at": "2020-01-01T00:00:00+00:00"}])
    assert pub[0]["action"] == "require_review"


def test_invalid_transitions_structured():
    with pytest.raises(StateTransitionError) as e:
        transition(CAMPAIGN_TRANSITIONS, "campaign", "COMPLETED", "ELIGIBLE")
    assert e.value.payload["previous_state"] == "COMPLETED"
    with pytest.raises(StateTransitionError):
        transition(SUBMISSION_TRANSITIONS, "submission", "CONFIRMED", "SUBMITTING")


def test_render_hash_deterministic_and_sensitive():
    kw = {"source_id": "s", "source_start": 1.0, "source_end": 9.0,
          "crop_keyframes": [{"t": 0}], "composition": {"canvas": "9:16"},
          "caption_style": "viral_word", "people_policy": {"mode": "BLUR_ALL"},
          "people_masks": [{"track_id": 1}], "title": "T", "output_aspect": "9:16"}
    assert compute_render_hash(**kw) == compute_render_hash(**kw)
    altered = dict(kw, source_end=9.5)
    assert compute_render_hash(**altered) != compute_render_hash(**kw)
