"""Automation E2E on mocks: scan → select → approve → dry-run → reports."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from automation import store
from automation.eligibility import CampaignEligibilityEngine
from automation.pipeline import scan_campaigns, select_clips, strongest_overlap_suppression
from automation.policy import UserCampaignPolicy
from automation.providers import MockCampaignProvider
from automation.publishing import MockPublisher
from automation.scheduler import AutomationScheduler, idempotency_key
from automation.analytics import aggregate_publications
from automation.reports import build_report_payload, send_email


def _use_tmp_db(tmp_path):
    database.DB_PATH = str(tmp_path / "auto_test.db")


def _camps():
    return [
        {"campaign_id": "good", "name": "Good launch",
         "description": "Fun product launch clips", "platforms": ["tiktok"],
         "content_sources": ["https://example.com/v"]},
        {"campaign_id": "bad", "name": "Bad casino",
         "description": "Online casino sports betting bonus", "platforms": ["tiktok"],
         "content_sources": ["https://example.com/v"]},
        {"campaign_id": "vague", "name": "Vague beats",
         "description": "Maybe betting odds, academic statistics lecture",
         "platforms": ["tiktok"], "content_sources": ["https://example.com/v"]},
    ]


def test_scan_filters_and_ranks(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    policy = UserCampaignPolicy()
    out = asyncio.run(scan_campaigns(MockCampaignProvider(_camps()),
                                      CampaignEligibilityEngine(policy=policy), store))
    ev = out["evaluations"]
    assert ev["good"]["status"] == "ELIGIBLE"
    assert ev["bad"]["status"] == "REJECTED"
    # educational-context campaign is ELIGIBLE (odds talk neutralized);
    # rejected campaigns are never ranked
    assert {r["campaign_id"] for r in out["ranked"]} == {"good", "vague"}
    assert "bad" not in [r["campaign_id"] for r in out["ranked"]]


def test_clip_selection_gates_and_overlap():
    policy = UserCampaignPolicy()
    cands = [
        {"clip_key": "a", "start": 0.0, "end": 20.0, "hook": 8, "payoff": 8,
         "context": 7, "audio": 7, "silence_ratio": 0.1},
        {"clip_key": "b", "start": 10.0, "end": 30.0, "hook": 4, "payoff": 4,
         "context": 4, "audio": 4, "silence_ratio": 0.1},  # weaker overlap → dropped
        {"clip_key": "m", "start": 40.0, "end": 55.0, "hook": 9, "payoff": 9,
         "context": 9, "audio": 9, "silence_ratio": 0.05},  # music → rejected
        {"clip_key": "s", "start": 60.0, "end": 80.0, "hook": 9, "payoff": 9,
         "context": 9, "audio": 9, "silence_ratio": 0.9},  # silence → rejected
        {"clip_key": "u", "start": 90.0, "end": 100.0, "hook": 9, "payoff": 9,
         "context": 9, "audio": 9, "silence_ratio": 0.0},  # people review
    ]
    music = {"m": {"status": "MUSIC_PRESENT", "confidence": 0.8}}
    gates = {"u": {"gate": "NEEDS_REVIEW", "reason": "uncertain_tracks"}}
    out = select_clips(cands, music, gates, policy, max_clips=5)
    keys = [s["clip_key"] for s in out["selected"]]
    assert "a" in keys and "b" not in keys  # strongest overlap kept
    assert "m" in [c["clip_key"] for c in out["rejected_music"]]
    assert "s" in [c["clip_key"] for c in out["rejected_silence"]]
    assert "u" in [c["clip_key"] for c in out["needs_review"]]


def test_strongest_overlap_not_earliest():
    cands = [{"start": 0, "end": 10, "hook": 2, "payoff": 2, "viral_score": 2},
             {"start": 5, "end": 15, "hook": 9, "payoff": 9, "viral_score": 9}]
    kept, suppressed = strongest_overlap_suppression(cands)
    assert len(kept) == 1 and kept[0]["start"] == 5
    assert len(suppressed) == 1 and suppressed[0]["reason"] == "OVERLAP_SUPPRESSED"


def test_dry_run_publishes_nothing_and_idempotent(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    key = idempotency_key("camp", "clip1", "hash1", "tiktok")
    assert key == idempotency_key("camp", "clip1", "hash1", "tiktok")
    pub = MockPublisher().publish("p.mp4", "t", "d", "tiktok", key)
    assert pub["mock"] and pub["post_url"].startswith("mock://")
    # duplicate prevention via store
    asyncio.run(store.record_publication("clip1", "camp", "tiktok", None,
                                         post_url="http://x/1",
                                         idempotency_key=key))
    asyncio.run(store.record_publication("clip1", "camp", "tiktok", None,
                                         post_url="http://x/1",
                                         idempotency_key=key))
    found = asyncio.run(store.find_publication_by_key(key))
    assert found and found["post_url"] == "http://x/1"
    pubs = asyncio.run(store.list_publications())
    assert sum(1 for p in pubs if p.get("idempotency_key") == key) == 1


def test_scheduler_resumable_steps(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    sched = AutomationScheduler(max_retries=1)
    ok = asyncio.run(sched.run_step("j1", "scan", lambda: _coro(5), store=store))
    assert ok["status"] == "completed" and ok["result"] == 5
    bad = asyncio.run(sched.run_step("j2", "scan", lambda: _coro_fail(), store=store))
    assert bad["status"] == "failed" and bad["error"]
    jobs = asyncio.run(store.list_jobs())
    assert {j["job_id"] for j in jobs} >= {"j1", "j2"}


async def _coro(v):
    return v


async def _coro_fail():
    raise RuntimeError("boom")


def test_approval_flow(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    asyncio.run(store.upsert_approval("clip9", "good", status="READY_FOR_APPROVAL",
                                      title="t", description="d"))
    asyncio.run(store.set_approval_status("clip9", "APPROVED", "owner"))
    rows = asyncio.run(store.list_approvals("APPROVED"))
    assert any(r["clip_id"] == "clip9" for r in rows)


def test_analytics_and_report_use_real_data_only():
    pubs = [{"id": "p1", "clip_id": "c1", "post_url": "http://x/1", "platform": "tiktok",
             "campaign_id": "good"}]
    snaps = [{"publication_id": "p1", "ts": "2026-01-01", "views": 100, "likes": 10,
              "comments": 2, "shares": 1, "earnings_confirmed": 2.5,
              "earnings_pending": 1.0}]
    summary = aggregate_publications(pubs, snaps)
    assert summary["total_views"] == 100 and summary["confirmed_earnings"] == 2.5
    assert summary["estimated_earnings"] == "NOT AVAILABLE"
    assert summary["top_clip"]["clip_id"] == "c1"
    empty = aggregate_publications([], [])
    assert empty["total_views"] == "NOT AVAILABLE"
    payload = build_report_payload("2026-01-05", "2026-01-11",
                                   [{"status": "ELIGIBLE"}, {"status": "REJECTED"}],
                                   [{"status": "APPROVED"}, {"status": "PUBLISHED"}],
                                   pubs, summary, [])
    assert payload["campaigns"]["eligible"] == 1
    assert payload["clips"]["published"] == 1
    assert send_email("s", "<p>x</p>", "x", []) == "EMAIL_DISABLED"
