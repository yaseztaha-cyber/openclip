"""Ops lifecycle tests (v1.2 §14-17, §22, §26): migration regression,
backup/restore, secret-flow audit, long-run stability. Isolated tmp DBs only.
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from automation import store

V10_TABLES = """
CREATE TABLE automation_campaigns (campaign_id TEXT PRIMARY KEY, name TEXT,
 payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
 reasons_json TEXT NOT NULL DEFAULT '[]', score REAL,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE automation_decisions (id TEXT PRIMARY KEY, ts TEXT NOT NULL,
 campaign_id TEXT, clip_id TEXT, decision TEXT NOT NULL,
 rules_json TEXT NOT NULL DEFAULT '[]', evidence_json TEXT NOT NULL DEFAULT '[]',
 operator TEXT NOT NULL DEFAULT 'automation');
CREATE TABLE automation_approvals (id TEXT PRIMARY KEY, clip_id TEXT NOT NULL,
 campaign_id TEXT, status TEXT NOT NULL DEFAULT 'DETECTED', title TEXT,
 description TEXT, preview_path TEXT, detail_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, decided_by TEXT, decided_at TEXT);
CREATE TABLE automation_publications (id TEXT PRIMARY KEY, clip_id TEXT NOT NULL,
 campaign_id TEXT, platform TEXT, account_id TEXT, published_at TEXT, post_url TEXT,
 submission_url TEXT, submission_status TEXT, status TEXT NOT NULL DEFAULT 'draft',
 idempotency_key TEXT UNIQUE);
CREATE TABLE automation_analytics (id TEXT PRIMARY KEY, clip_id TEXT,
 publication_id TEXT, ts TEXT NOT NULL, views INTEGER, likes INTEGER, comments INTEGER,
 shares INTEGER, watch_time REAL, earnings REAL, raw_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE automation_reports (report_id TEXT PRIMARY KEY, week_start TEXT NOT NULL,
 week_end TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
 report_html TEXT NOT NULL DEFAULT '', email_status TEXT NOT NULL DEFAULT 'EMAIL NOT SENT',
 created_at TEXT NOT NULL);
CREATE TABLE automation_people_tracks (clip_id TEXT NOT NULL, track_id INTEGER NOT NULL,
 start_time REAL NOT NULL, end_time REAL NOT NULL, boxes_json TEXT NOT NULL DEFAULT '[]',
 policy_status TEXT NOT NULL DEFAULT 'UNCERTAIN', confidence REAL NOT NULL DEFAULT 0.0,
 action TEXT NOT NULL DEFAULT 'REVIEW', reviewer TEXT, review_time TEXT,
 PRIMARY KEY (clip_id, track_id));
CREATE TABLE automation_jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
 campaign_id TEXT, clip_id TEXT, status TEXT NOT NULL DEFAULT 'queued',
 started_at TEXT, completed_at TEXT, error TEXT, payload_json TEXT NOT NULL DEFAULT '{}');
"""


def _fresh_db(tmp_path, name="ops.db"):
    database.DB_PATH = str(tmp_path / name)
    asyncio.run(store.init_automation_tables())
    return database.DB_PATH


def _cols(db, table):
    c = sqlite3.connect(db)
    try:
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        c.close()


def test_fresh_installation_has_full_schema(tmp_path):
    db = _fresh_db(tmp_path, "fresh.db")
    assert "render_hash" in _cols(db, "automation_approvals")
    assert "attempt_number" in _cols(db, "automation_submission_attempts")
    assert "policy_hash" in _cols(db, "automation_policies")
    assert "previous_state" in _cols(db, "automation_transitions")


def test_v10_migration_preserves_data(tmp_path):
    db = str(tmp_path / "v10.db")
    database.DB_PATH = db
    c = sqlite3.connect(db)
    c.executescript(V10_TABLES)
    c.execute("INSERT INTO automation_campaigns (campaign_id, name, status) VALUES ('c1','C','ELIGIBLE')")
    c.execute("INSERT INTO automation_approvals (id, clip_id, status, created_at, updated_at)"
              " VALUES ('appr_x','x','APPROVED','t','t')")
    c.execute("INSERT INTO automation_jobs (job_id, kind, status) VALUES ('j1','render','completed')")
    c.commit()
    c.close()
    asyncio.run(store.init_automation_tables())
    assert "render_hash" in _cols(db, "automation_approvals")
    assert "attempt" in _cols(db, "automation_jobs")
    assert "automation_transitions" in [
        r[0] for r in sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT status FROM automation_campaigns WHERE campaign_id='c1'").fetchone()[0] == "ELIGIBLE"
        assert c.execute("SELECT status FROM automation_approvals WHERE clip_id='x'").fetchone()[0] == "APPROVED"
        assert c.execute("SELECT status FROM automation_jobs WHERE job_id='j1'").fetchone()[0] == "completed"
    finally:
        c.close()


def test_v11_migration_adds_attempts_preserving_history(tmp_path):
    db = str(tmp_path / "v11.db")
    database.DB_PATH = db
    c = sqlite3.connect(db)
    c.executescript(V10_TABLES)
    c.executescript("CREATE TABLE automation_transitions (id TEXT PRIMARY KEY, ts TEXT NOT NULL,"
                    " machine TEXT NOT NULL, previous_state TEXT, new_state TEXT NOT NULL,"
                    " actor TEXT NOT NULL DEFAULT 'automation', reason TEXT NOT NULL DEFAULT '',"
                    " job_id TEXT, clip_id TEXT, campaign_id TEXT, render_id TEXT);"
                    "CREATE TABLE automation_policies (policy_id TEXT PRIMARY KEY,"
                    " version INTEGER NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',"
                    " policy_hash TEXT NOT NULL DEFAULT '', owner_actor TEXT NOT NULL DEFAULT 'owner',"
                    " created_at TEXT NOT NULL);")
    c.execute("INSERT INTO automation_transitions (id, ts, machine, new_state) VALUES ('t1','t','clip','APPROVED')")
    c.execute("INSERT INTO automation_policies (policy_id, version, created_at) VALUES ('p1',1,'t')")
    c.commit()
    c.close()
    asyncio.run(store.init_automation_tables())
    assert "automation_submission_attempts" in [
        r[0] for r in sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT new_state FROM automation_transitions WHERE id='t1'").fetchone()[0] == "APPROVED"
        assert c.execute("SELECT version FROM automation_policies WHERE policy_id='p1'").fetchone()[0] == 1
    finally:
        c.close()


def test_backup_restore_preserves_state(tmp_path):
    db = _fresh_db(tmp_path, "prod.db")
    asyncio.run(store.upsert_campaign("c9", "C", {"campaign_id": "c9"}, "ELIGIBLE", []))
    asyncio.run(store.upsert_approval("clip9", "c9", status="APPROVED", title="t"))
    asyncio.run(store.claim_publication_slot("clip9", "c9", "tiktok", "KEY9"))
    asyncio.run(store.update_publication_by_key("KEY9", status="unknown",
                                                submission_state="UNKNOWN_EXTERNAL_RESULT"))
    asyncio.run(store.record_submission_attempt(clip_id="clip9", campaign_id="c9",
                                                platform="tiktok", idempotency_key="KEY9"))
    asyncio.run(store.log_decision(campaign_id="c9", decision="eligible", rules=[],
                                   evidence=[], operator="automation"))
    # backup via sqlite backup API (never mutates source beyond reading)
    backup = str(tmp_path / "backup.db")
    src = sqlite3.connect(db)
    try:
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    database.DB_PATH = backup
    asyncio.run(store.init_automation_tables())  # idempotent on restored copy
    assert (asyncio.run(store.get_approval("clip9")) or {})["status"] == "APPROVED"
    pub = asyncio.run(store.find_publication_by_key("KEY9"))
    assert pub["submission_state"] == "UNKNOWN_EXTERNAL_RESULT"  # still unresolved
    assert len(asyncio.run(store.list_submission_attempts("KEY9"))) == 1
    assert asyncio.run(store.list_decisions("c9")) != []
    # scheduler can resume: unknown preserved, never auto-rerun
    from automation.scheduler import AutomationScheduler
    rec = AutomationScheduler.recover_interrupted(
        [{"job_id": "p9", "status": "running", "kind": "publish_clip",
          "attempt": 3, "heartbeat_at": "2020-01-01T00:00:00+00:00",
          "payload_json": "{\"submission_state\": \"UNKNOWN_EXTERNAL_RESULT\"}"}])
    assert rec[0]["action"] == "require_review"


def test_secret_flow_redaction(tmp_path):
    _fresh_db(tmp_path, "sec.db")
    token = "tok_test_secret_abc123"
    os.environ["PUBLISH_TOKEN_TIKTOK"] = token
    try:
        asyncio.run(store.record_submission_attempt(
            clip_id="c", campaign_id="c", platform="tiktok",
            idempotency_key="KS"))
        atts = asyncio.run(store.list_submission_attempts("KS"))
        aid = atts[0]["submission_attempt_id"]
        asyncio.run(store.finish_submission_attempt(
            aid, request_status="RESPONDED", result_state="FAILED",
            provider_submission_id="s1",
            provider_response_metadata={"ok": True, "auth_token": token,
                                        "nested": {"x": 1}},
            error_code="e", error_message="m"))
        from automation import events as _ev
        _ev.emit("SUBMISSION_STARTED", clip_id="c",
                 details={"token": token, "ok": True})
        blob_parts = [
            open(database.DB_PATH, "rb").read().decode("utf-8", "ignore"),
            str(asyncio.run(store.list_submission_attempts("KS"))),
        ]
        for blob in blob_parts:
            assert token not in blob
    finally:
        del os.environ["PUBLISH_TOKEN_TIKTOK"]


def test_long_run_stability(tmp_path):
    db = _fresh_db(tmp_path, "stable.db")
    from automation.eligibility import CampaignEligibilityEngine
    from automation.pipeline import scan_campaigns, select_clips
    from automation.policy import UserCampaignPolicy
    from automation.providers import MockCampaignProvider
    from automation.scheduler import AutomationScheduler
    from automation.submissions import reconcile_one, LookupUnavailable
    from automation.reports import (build_report_payload, render_html,
                                    render_text, report_hash)
    camps = [{"campaign_id": "s1", "name": "S",
              "description": "Fun launch", "platforms": ["tiktok"],
              "content_sources": ["https://example.com/v"]}]
    cands = [{"clip_key": "a", "start": 0.0, "end": 10.0, "hook": 7,
              "payoff": 7, "context": 6, "audio": 6, "silence_ratio": 0.1}]
    sched = AutomationScheduler(max_retries=1)

    async def _cycle(i: int):
        await scan_campaigns(MockCampaignProvider(camps),
                             CampaignEligibilityEngine(policy=UserCampaignPolicy()),
                             store, policy_version=1)
        select_clips(cands, {}, {}, UserCampaignPolicy(), seed=i)
        await sched.run_step(f"job{i}", "render", _ok, store=store)

        async def _boom():
            raise RuntimeError("mock provider failure")

        await sched.run_step(f"jobf{i}", "scan", _boom, store=store)
        pubs = await store.list_publications()
        summary = {"published_count": 0, "total_views": "NOT AVAILABLE",
                   "engagement": 0, "confirmed_earnings": "NOT AVAILABLE",
                   "pending_earnings": "NOT AVAILABLE",
                   "estimated_earnings": "NOT AVAILABLE", "top_clip": None}
        payload = build_report_payload("2026-01-05", "2026-01-11",
                                       await store.list_campaigns(),
                                       await store.list_approvals(),
                                       pubs, summary, [])
        rh = report_hash(payload)
        if not await store.find_report_by_hash(rh):
            await store.save_report(f"rep_stable_{i}", "2026-01-05",
                                    "2026-01-11", payload, "<p>x</p>",
                                    "EMAIL_DISABLED", report_hash=rh)

    async def _ok():
        return "fine"

    for i in range(5):
        asyncio.run(_cycle(i))

    async def _nolookup(**kw):
        raise LookupUnavailable("x")

    asyncio.run(store.claim_publication_slot("c", "s1", "tiktok", "STABLE_K"))
    asyncio.run(store.update_publication_by_key("STABLE_K", status="unknown",
                                                submission_state="UNKNOWN_EXTERNAL_RESULT"))
    for _ in range(3):
        res = asyncio.run(reconcile_one(store, _nolookup, "STABLE_K", actor="test"))
        assert res["outcome"] == "STILL_UNKNOWN"

    c = sqlite3.connect(db)
    try:
        n_reports = c.execute("SELECT COUNT(*) FROM automation_reports").fetchone()[0]
        assert n_reports == 1, f"duplicate reports: {n_reports}"
        running = c.execute("SELECT COUNT(*) FROM automation_jobs WHERE status='running'").fetchone()[0]
        assert running == 0
        n_failed = c.execute("SELECT COUNT(*) FROM automation_jobs WHERE status='failed'").fetchone()[0]
        assert n_failed == 5
        n_camps = c.execute("SELECT COUNT(*) FROM automation_campaigns").fetchone()[0]
        assert n_camps == 1
    finally:
        c.close()
