"""
store.py — Automation-hub persistence (same SQLite file, new tables).

Tables: automation_campaigns, automation_decisions (audit), automation_approvals,
automation_publications, automation_analytics, automation_reports,
automation_people_tracks, automation_jobs.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid

import database  # type: ignore

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


async def init_automation_tables() -> None:
    conn = await database._get_connection()
    try:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS automation_campaigns (
                campaign_id TEXT PRIMARY KEY,
                name TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
                reasons_json TEXT NOT NULL DEFAULT '[]',
                score REAL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS automation_decisions (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                campaign_id TEXT,
                clip_id TEXT,
                decision TEXT NOT NULL,
                rules_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                operator TEXT NOT NULL DEFAULT 'automation'
            );
            CREATE TABLE IF NOT EXISTS automation_approvals (
                id TEXT PRIMARY KEY,
                clip_id TEXT NOT NULL,
                campaign_id TEXT,
                status TEXT NOT NULL DEFAULT 'DETECTED',
                title TEXT,
                description TEXT,
                preview_path TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS automation_publications (
                id TEXT PRIMARY KEY,
                clip_id TEXT NOT NULL,
                campaign_id TEXT,
                platform TEXT,
                account_id TEXT,
                published_at TEXT,
                post_url TEXT,
                submission_url TEXT,
                submission_status TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                idempotency_key TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS automation_analytics (
                id TEXT PRIMARY KEY,
                clip_id TEXT,
                publication_id TEXT,
                ts TEXT NOT NULL,
                views INTEGER,
                likes INTEGER,
                comments INTEGER,
                shares INTEGER,
                watch_time REAL,
                earnings REAL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS automation_reports (
                report_id TEXT PRIMARY KEY,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                report_html TEXT NOT NULL DEFAULT '',
                email_status TEXT NOT NULL DEFAULT 'EMAIL NOT SENT',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automation_people_tracks (
                clip_id TEXT NOT NULL,
                track_id INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                boxes_json TEXT NOT NULL DEFAULT '[]',
                policy_status TEXT NOT NULL DEFAULT 'UNCERTAIN',
                confidence REAL NOT NULL DEFAULT 0.0,
                action TEXT NOT NULL DEFAULT 'REVIEW',
                reviewer TEXT,
                review_time TEXT,
                PRIMARY KEY (clip_id, track_id)
            );
            CREATE TABLE IF NOT EXISTS automation_jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                campaign_id TEXT,
                clip_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                started_at TEXT,
                completed_at TEXT,
                error TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS automation_transitions (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                machine TEXT NOT NULL,
                previous_state TEXT,
                new_state TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'automation',
                reason TEXT NOT NULL DEFAULT '',
                job_id TEXT,
                clip_id TEXT,
                campaign_id TEXT,
                render_id TEXT
            );
            CREATE TABLE IF NOT EXISTS automation_policies (
                policy_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                policy_hash TEXT NOT NULL DEFAULT '',
                owner_actor TEXT NOT NULL DEFAULT 'owner',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automation_submission_attempts (
                submission_attempt_id TEXT PRIMARY KEY,
                campaign_id TEXT,
                clip_id TEXT,
                render_id TEXT,
                platform TEXT,
                idempotency_key TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                request_status TEXT NOT NULL DEFAULT 'STARTED',
                provider_submission_id TEXT,
                provider_response_status TEXT,
                provider_response_metadata TEXT NOT NULL DEFAULT '{}',
                result_state TEXT NOT NULL DEFAULT 'UNKNOWN_EXTERNAL_RESULT',
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_idem
                ON automation_submission_attempts (idempotency_key, attempt_number);
            CREATE TABLE IF NOT EXISTS automation_provider_qualifications (                record_id TEXT PRIMARY KEY,
                qualification_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                platform TEXT NOT NULL,
                adapter_version TEXT NOT NULL DEFAULT 'v1',
                client_version TEXT NOT NULL DEFAULT 'v1',
                capabilities_json TEXT NOT NULL DEFAULT '{}',
                configuration_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                authentication_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                availability_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                operational_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                last_checked TEXT NOT NULL,
                verification_type TEXT NOT NULL DEFAULT 'UNAVAILABLE',
                verification_scope TEXT NOT NULL DEFAULT '',
                limitations TEXT NOT NULL DEFAULT '',
                required_credentials_json TEXT NOT NULL DEFAULT '[]',
                checks_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automation_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS automation_release_acceptance (
                id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 0,
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                checklist_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.commit()
        # --- v1 hardening migrations (additive only; never delete data) ---
        for table, col, ctype in (
            ("automation_approvals", "render_id", "TEXT"),
            ("automation_approvals", "render_hash", "TEXT"),
            ("automation_approvals", "policy_version", "INTEGER"),
            ("automation_approvals", "title_hash", "TEXT"),
            ("automation_approvals", "description_hash", "TEXT"),
            ("automation_approvals", "approval_version", "INTEGER NOT NULL DEFAULT 1"),
            ("automation_approvals", "expires_at", "TEXT"),
            ("automation_approvals", "invalidated_reason", "TEXT"),
            ("automation_approvals", "metadata_version", "INTEGER NOT NULL DEFAULT 1"),
            ("automation_publications", "submission_state", "TEXT NOT NULL DEFAULT 'NOT_SUBMITTED'"),
            ("automation_analytics", "provider", "TEXT"),
            ("automation_analytics", "source", "TEXT"),
            ("automation_analytics", "observed_status", "TEXT"),
            ("automation_reports", "report_hash", "TEXT"),
            ("automation_reports", "delivery_id", "TEXT"),
            ("automation_jobs", "attempt", "INTEGER NOT NULL DEFAULT 0"),
            ("automation_jobs", "step", "TEXT"),
            ("automation_jobs", "heartbeat_at", "TEXT"),
            ("automation_jobs", "lease_owner", "TEXT"),
            ("automation_jobs", "failure_code", "TEXT"),
            ("automation_campaigns", "policy_version", "INTEGER"),
            ("automation_decisions", "policy_version", "INTEGER"),
            ("automation_people_tracks", "blur", "INTEGER NOT NULL DEFAULT 0"),
            ("automation_people_tracks", "source", "TEXT NOT NULL DEFAULT 'automatic'"),
            ("automation_people_tracks", "review_status", "TEXT NOT NULL DEFAULT 'PENDING'"),
            ("automation_approvals", "platform", "TEXT"),
        ):
            try:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
                await conn.commit()
            except Exception:
                pass  # already migrated
    finally:
        await conn.close()


async def upsert_campaign(campaign_id: str, name: str, payload: dict,
                          status: str, reasons: list, score=None) -> None:
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_campaigns (campaign_id, name, payload_json, status, reasons_json, score, updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(campaign_id) DO UPDATE SET name=excluded.name, payload_json=excluded.payload_json,"
            " status=excluded.status, reasons_json=excluded.reasons_json, score=excluded.score, updated_at=excluded.updated_at",
            (campaign_id, name, json.dumps(payload), status, json.dumps(reasons),
             score, _now()))
        await conn.commit()
    finally:
        await conn.close()


async def list_campaigns(status: str | None = None) -> list[dict]:
    conn = await database._get_connection()
    try:
        if status:
            cur = await conn.execute(
                "SELECT * FROM automation_campaigns WHERE status=? ORDER BY score DESC NULLS LAST, updated_at DESC",
                (status,))
        else:
            cur = await conn.execute(
                "SELECT * FROM automation_campaigns ORDER BY updated_at DESC")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def delete_campaign(campaign_id: str) -> None:
    conn = await database._get_connection()
    try:
        await conn.execute("DELETE FROM automation_campaigns WHERE campaign_id=?",
                           (campaign_id,))
        await conn.commit()
    finally:
        await conn.close()


async def log_decision(campaign_id=None, clip_id=None, decision="", rules=None,
                       evidence=None, operator="automation") -> str:
    did = uuid.uuid4().hex[:12]
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_decisions (id, ts, campaign_id, clip_id, decision, rules_json, evidence_json, operator)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (did, _now(), campaign_id, clip_id, decision,
             json.dumps(rules or []), json.dumps(evidence or []), operator))
        await conn.commit()
        return did
    finally:
        await conn.close()


async def list_decisions(campaign_id: str | None = None, limit: int = 200) -> list[dict]:
    conn = await database._get_connection()
    try:
        if campaign_id:
            cur = await conn.execute(
                "SELECT * FROM automation_decisions WHERE campaign_id=? ORDER BY ts DESC LIMIT ?",
                (campaign_id, limit))
        else:
            cur = await conn.execute(
                "SELECT * FROM automation_decisions ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def upsert_approval(clip_id: str, campaign_id=None, status="DETECTED",
                          title=None, description=None, preview_path=None,
                          detail=None) -> str:
    aid = f"appr_{clip_id}"
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_approvals (id, clip_id, campaign_id, status, title, description, preview_path, detail_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET status=excluded.status, title=excluded.title,"
            " description=excluded.description, preview_path=excluded.preview_path,"
            " detail_json=excluded.detail_json, updated_at=excluded.updated_at",
            (aid, clip_id, campaign_id, status, title, description, preview_path,
             json.dumps(detail or {}), _now(), _now()))
        await conn.commit()
        return aid
    finally:
        await conn.close()


async def set_approval_status(clip_id: str, status: str, decided_by=None) -> None:
    conn = await database._get_connection()
    try:
        await conn.execute(
            "UPDATE automation_approvals SET status=?, updated_at=?, decided_by=?, decided_at=? WHERE clip_id=?",
            (status, _now(), decided_by, _now(), clip_id))
        await conn.commit()
    finally:
        await conn.close()


async def bind_approval_render(clip_id: str, *, render_id=None, render_hash=None,
                               policy_version=None, title_hash=None,
                               description_hash=None, platform=None,
                               ttl_hours=None) -> None:
    """
    Bind an approval to an exact immutable content identity. Any later
    render/metadata change produces a different hash, which the publish
    gate rejects (stale approvals can never authorize new renders).
    """
    import datetime as _dt
    import os as _os
    if ttl_hours is None:
        try:
            ttl_hours = float(_os.getenv("APPROVAL_TTL_HOURS", "168"))
        except Exception:
            ttl_hours = 168.0
    expires = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(hours=ttl_hours)).isoformat()
    conn = await database._get_connection()
    try:
        await conn.execute(
            "UPDATE automation_approvals SET render_id=COALESCE(?, render_id),"
            " render_hash=COALESCE(?, render_hash),"
            " policy_version=COALESCE(?, policy_version),"
            " title_hash=COALESCE(?, title_hash),"
            " description_hash=COALESCE(?, description_hash),"
            " platform=COALESCE(?, platform),"
            " approval_version=COALESCE(approval_version,1)+1,"
            " expires_at=?, invalidated_reason=NULL, updated_at=? WHERE clip_id=?",
            (render_id, render_hash, policy_version, title_hash,
             description_hash, (platform or "").strip().lower() or None,
             expires, _now(), clip_id))
        await conn.commit()
    finally:
        await conn.close()


async def invalidate_approval(clip_id: str, reason: str) -> None:
    conn = await database._get_connection()
    try:
        await conn.execute(
            "UPDATE automation_approvals SET invalidated_reason=?, updated_at=? WHERE clip_id=?",
            (reason, _now(), clip_id))
        await conn.commit()
    finally:
        await conn.close()


async def list_approvals(status: str | None = None) -> list[dict]:
    conn = await database._get_connection()
    try:
        if status:
            cur = await conn.execute(
                "SELECT * FROM automation_approvals WHERE status=? ORDER BY updated_at DESC", (status,))
        else:
            cur = await conn.execute("SELECT * FROM automation_approvals ORDER BY updated_at DESC")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def record_publication(clip_id: str, campaign_id=None, platform=None,
                             account_id=None, post_url=None, submission_url=None,
                             submission_status=None, status="published",
                             idempotency_key=None) -> str:
    pid = uuid.uuid4().hex[:12]
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_publications (id, clip_id, campaign_id, platform, account_id, published_at,"
            " post_url, submission_url, submission_status, status, idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(idempotency_key) DO NOTHING",
            (pid, clip_id, campaign_id, platform, account_id, _now(), post_url,
             submission_url, submission_status, status, idempotency_key))
        await conn.commit()
        return pid
    finally:
        await conn.close()


async def find_publication_by_key(idempotency_key: str) -> dict | None:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_publications WHERE idempotency_key=?", (idempotency_key,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def list_publications() -> list[dict]:
    conn = await database._get_connection()
    try:
        cur = await conn.execute("SELECT * FROM automation_publications ORDER BY published_at DESC")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def record_analytics(clip_id=None, publication_id=None, metrics=None,
                           provider=None, source=None, observed_status=None) -> str:
    from automation.submissions import redact_metadata
    metrics = redact_metadata(metrics or {})
    aid = uuid.uuid4().hex[:12]
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_analytics (id, clip_id, publication_id, ts, views, likes, comments, shares,"
            " watch_time, earnings, raw_json, provider, source, observed_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, clip_id, publication_id, _now(), metrics.get("views"), metrics.get("likes"),
             metrics.get("comments"), metrics.get("shares"), metrics.get("watch_time"),
             metrics.get("earnings"), json.dumps(metrics), provider, source,
             observed_status))
        await conn.commit()
        return aid
    finally:
        await conn.close()


async def latest_analytics() -> list[dict]:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_analytics ORDER BY ts DESC LIMIT 500")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def save_report(report_id: str, week_start: str, week_end: str, payload: dict,
                      html: str, email_status: str, report_hash=None,
                      delivery_id=None) -> None:
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT OR REPLACE INTO automation_reports (report_id, week_start, week_end, payload_json, report_html, email_status, created_at,"
            " report_hash, delivery_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, week_start, week_end, json.dumps(payload), html, email_status, _now(),
             report_hash, delivery_id))
        await conn.commit()
    finally:
        await conn.close()


async def list_reports() -> list[dict]:
    conn = await database._get_connection()
    try:
        cur = await conn.execute("SELECT * FROM automation_reports ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def save_people_tracks(clip_id: str, tracks: list[dict]) -> None:
    conn = await database._get_connection()
    try:
        for t in tracks:
            await conn.execute(
                "INSERT INTO automation_people_tracks (clip_id, track_id, start_time, end_time, boxes_json,"
                " policy_status, confidence, action, reviewer, review_time,"
                " blur, source, review_status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(clip_id, track_id) DO UPDATE SET start_time=excluded.start_time,"
                " end_time=excluded.end_time, boxes_json=excluded.boxes_json,"
                " policy_status=excluded.policy_status, confidence=excluded.confidence,"
                " action=excluded.action, reviewer=excluded.reviewer, review_time=excluded.review_time,"
                " blur=excluded.blur, source=excluded.source, review_status=excluded.review_status",
                (clip_id, int(t.get("track_id", 0)), float(t.get("start_time", 0)),
                 float(t.get("end_time", 0)), json.dumps(t.get("boxes", [])),
                 t.get("policy_status", "DETECTED"), float(t.get("confidence", 0)),
                 t.get("action", "KEEP"), t.get("reviewer"), t.get("review_time"),
                 1 if t.get("blur") else 0, t.get("source", "automatic"),
                 t.get("review_status", "PENDING")))
        await conn.commit()
    finally:
        await conn.close()


async def list_people_tracks(clip_id: str) -> list[dict]:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_people_tracks WHERE clip_id=? ORDER BY track_id", (clip_id,))
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def review_person_track(clip_id: str, track_id: int, action: str, reviewer: str) -> None:
    conn = await database._get_connection()
    try:
        await conn.execute(
            "UPDATE automation_people_tracks SET action=?, reviewer=?, review_time=? WHERE clip_id=? AND track_id=?",
            (action, reviewer, _now(), clip_id, track_id))
        await conn.commit()
    finally:
        await conn.close()


async def upsert_job(job_id: str, kind: str, status: str, campaign_id=None,
                     clip_id=None, error=None, payload=None, step=None,
                     failure_code=None) -> None:
    conn = await database._get_connection()
    try:
        now = _now()
        finished = now if status in ("completed", "failed", "cancelled") else None
        payload = dict(payload or {})
        if failure_code and "failure_code" not in payload:
            payload["failure_code"] = failure_code
        attempt = int(payload.get("attempts", payload.get("attempt", 0)) or 0)
        await conn.execute(
            "INSERT INTO automation_jobs (job_id, kind, campaign_id, clip_id, status, started_at, completed_at, error, payload_json,"
            " attempt, step, heartbeat_at, failure_code)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, completed_at=?, error=excluded.error,"
            " payload_json=excluded.payload_json, attempt=?, step=?, failure_code=?",
            (job_id, kind, campaign_id, clip_id, status, now, finished, error,
             json.dumps(payload), attempt, step, now, payload.get("failure_code"),
             finished, attempt, step, payload.get("failure_code")))
        await conn.commit()
    finally:
        await conn.close()


async def list_jobs(limit: int = 100) -> list[dict]:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_jobs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# v1 hardening primitives (Phases 1, 4, 5, 13, 14, 16)
# ---------------------------------------------------------------------------

async def record_transition(rec) -> str:
    """Persist a validated TransitionRecord. Returns transition id."""
    tid = uuid.uuid4().hex[:12]
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_transitions (id, ts, machine, previous_state, new_state,"
            " actor, reason, job_id, clip_id, campaign_id, render_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, rec.timestamp, rec.machine, rec.previous_state, rec.new_state,
             rec.actor, rec.reason, rec.job_id, rec.clip_id, rec.campaign_id,
             rec.render_id))
        await conn.commit()
        return tid
    finally:
        await conn.close()


async def list_transitions(clip_id=None, campaign_id=None, limit: int = 200) -> list[dict]:
    conn = await database._get_connection()
    try:
        if clip_id:
            cur = await conn.execute(
                "SELECT * FROM automation_transitions WHERE clip_id=? ORDER BY ts DESC LIMIT ?",
                (clip_id, limit))
        elif campaign_id:
            cur = await conn.execute(
                "SELECT * FROM automation_transitions WHERE campaign_id=? ORDER BY ts DESC LIMIT ?",
                (campaign_id, limit))
        else:
            cur = await conn.execute(
                "SELECT * FROM automation_transitions ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def get_approval(clip_id: str) -> dict | None:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_approvals WHERE clip_id=?", (clip_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def save_policy_version(payload: dict, policy_hash: str, owner_actor: str = "owner") -> dict:
    """Append a new immutable policy version. Returns {policy_id, version}."""
    conn = await database._get_connection()
    try:
        cur = await conn.execute("SELECT MAX(version) AS v FROM automation_policies")
        row = await cur.fetchone()
        version = int((row["v"] if row and row["v"] is not None else 0)) + 1
        pid = f"pol_{uuid.uuid4().hex[:10]}"
        await conn.execute(
            "INSERT INTO automation_policies (policy_id, version, payload_json, policy_hash, owner_actor, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (pid, version, json.dumps(payload), policy_hash, owner_actor, _now()))
        await conn.commit()
        return {"policy_id": pid, "version": version, "policy_hash": policy_hash}
    finally:
        await conn.close()


async def latest_policy_version() -> dict | None:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_policies ORDER BY version DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def list_policy_versions() -> list[dict]:
    conn = await database._get_connection()
    try:
        cur = await conn.execute("SELECT * FROM automation_policies ORDER BY version DESC")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


class IncompatibleSchema(Exception):
    """Raised at startup when the DB schema is NEWER than this build."""


async def get_meta(key: str) -> str | None:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT value FROM automation_meta WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None
    finally:
        await conn.close()


async def ensure_schema_compatible() -> dict:
    """Stamp current versions; fail LOUDLY on newer-than-code schema."""
    import version as _version
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT value FROM automation_meta WHERE key='schema_version'")
        row = await cur.fetchone()
        stored = int(row["value"]) if row and str(row["value"]).isdigit() else 0
        if stored > _version.SCHEMA_VERSION:
            raise IncompatibleSchema(
                f"database schema v{stored} is newer than this build "
                f"(v{_version.SCHEMA_VERSION}); refusing to downgrade")
        await conn.execute(
            "INSERT INTO automation_meta (key, value, updated_at)"
            " VALUES ('schema_version', ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=CURRENT_TIMESTAMP",
            (str(_version.SCHEMA_VERSION),))
        await conn.execute(
            "INSERT INTO automation_meta (key, value, updated_at)"
            " VALUES ('app_version', ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=CURRENT_TIMESTAMP",
            (_version.APP_VERSION,))
        await conn.commit()
        return {"schema_version": _version.SCHEMA_VERSION,
                "app_version": _version.APP_VERSION,
                "previous_schema_version": stored}
    finally:
        await conn.close()


async def claim_publication_slot(clip_id: str, campaign_id, platform: str,
                                 idempotency_key: str) -> bool:
    """
    Atomically claim the idempotent publication slot. Returns True exactly
    once per key (survives double-click, retry, restart, concurrent workers);
    False means the slot is already claimed (ALREADY_PUBLISHED path).
    """
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "INSERT INTO automation_publications (id, clip_id, campaign_id, platform,"
            " published_at, status, submission_state, idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
            (f"pub_{uuid.uuid4().hex[:10]}", clip_id, campaign_id, platform,
             _now(), "claimed", "NOT_SUBMITTED", idempotency_key))
        await conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        await conn.close()


async def update_publication_by_key(idempotency_key: str, **fields) -> None:
    allowed = {"status", "post_url", "submission_url", "submission_status",
               "submission_state", "account_id", "published_at"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    conn = await database._get_connection()
    try:
        cols = ", ".join(f"{k}=?" for k in sets)
        await conn.execute(
            f"UPDATE automation_publications SET {cols} WHERE idempotency_key=?",
            (*sets.values(), idempotency_key))
        await conn.commit()
    finally:
        await conn.close()


async def heartbeat_job(job_id: str, lease_owner: str, step: str | None = None) -> None:
    conn = await database._get_connection()
    try:
        await conn.execute(
            "UPDATE automation_jobs SET heartbeat_at=?, lease_owner=?, step=? WHERE job_id=?",
            (_now(), lease_owner, step, job_id))
        await conn.commit()
    finally:
        await conn.close()


async def get_job(job_id: str) -> dict | None:
    conn = await database._get_connection()
    try:
        cur = await conn.execute("SELECT * FROM automation_jobs WHERE job_id=?", (job_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def save_qualification(record: dict) -> str:
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_provider_qualifications (record_id, qualification_id,"
            " provider, platform, adapter_version, client_version, capabilities_json,"
            " configuration_status, authentication_status, availability_status,"
            " operational_status, last_checked, verification_type, verification_scope,"
            " limitations, required_credentials_json, checks_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["record_id"], record["qualification_id"], record["provider"],
             record["platform"], record.get("adapter_version", "v1"),
             record.get("client_version", "v1"),
             json.dumps(record.get("capabilities", {})),
             record.get("configuration_status", "UNKNOWN"),
             record.get("authentication_status", "UNKNOWN"),
             record.get("availability_status", "UNKNOWN"),
             record.get("operational_status", "UNKNOWN"),
             record.get("last_checked", _now()),
             record.get("verification_type", "UNAVAILABLE"),
             record.get("verification_scope", ""),
             record.get("limitations", ""),
             json.dumps(record.get("required_credentials", [])),
             json.dumps(record.get("checks", {})), _now()))
        await conn.commit()
        return record["record_id"]
    finally:
        await conn.close()


async def list_qualifications(provider=None, platform=None) -> list[dict]:
    conn = await database._get_connection()
    try:
        q = "SELECT * FROM automation_provider_qualifications"
        args: list = []
        clauses = []
        if provider:
            clauses.append("provider=?")
            args.append(provider)
        if platform:
            clauses.append("platform=?")
            args.append(platform)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC"
        cur = await conn.execute(q, args)
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            mapping = {"capabilities_json": ("capabilities", {}),
                       "required_credentials_json": ("required_credentials", []),
                       "checks_json": ("checks", {})}
            for col, (key, default) in mapping.items():
                try:
                    d[key] = json.loads(d.pop(col) or json.dumps(default))
                except Exception:
                    d[key] = default
            out.append(d)
        return out
    finally:
        await conn.close()


async def record_acceptance(version: str, schema_version: int, actor: str,
                            checklist: dict, notes: str = "") -> str:
    """Explicit operator acceptance event. Never created automatically."""
    aid = f"acc_{uuid.uuid4().hex[:10]}"
    conn = await database._get_connection()
    try:
        await conn.execute(
            "INSERT INTO automation_release_acceptance (id, version, schema_version,"
            " actor, timestamp, checklist_json, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (aid, version, schema_version, actor, _now(),
             json.dumps(checklist), notes, _now()))
        await conn.commit()
        return aid
    finally:
        await conn.close()


async def list_acceptances(version: str | None = None) -> list[dict]:
    conn = await database._get_connection()
    try:
        if version:
            cur = await conn.execute(
                "SELECT * FROM automation_release_acceptance WHERE version=?"
                " ORDER BY created_at DESC", (version,))
        else:
            cur = await conn.execute(
                "SELECT * FROM automation_release_acceptance ORDER BY created_at DESC")
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["checklist"] = json.loads(d.pop("checklist_json") or "{}")
            except Exception:
                d["checklist"] = {}
            out.append(d)
        return out
    finally:
        await conn.close()


async def find_report_by_hash(report_hash: str) -> dict | None:
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_reports WHERE report_hash=? ORDER BY created_at DESC LIMIT 1",
            (report_hash,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def record_submission_attempt(*, campaign_id=None, clip_id=None,
                                    render_id=None, platform=None,
                                    idempotency_key: str,
                                    request_status: str = "STARTED") -> dict:
    """Persist a new attempt row with the next attempt number. Returns the row."""
    from automation.submissions import new_attempt_id
    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) AS n"
            " FROM automation_submission_attempts WHERE idempotency_key=?",
            (idempotency_key,))
        row = await cur.fetchone()
        number = int(row["n"] if row else 0) + 1
        aid = new_attempt_id()
        now = _now()
        await conn.execute(
            "INSERT INTO automation_submission_attempts (submission_attempt_id,"
            " campaign_id, clip_id, render_id, platform, idempotency_key,"
            " attempt_number, started_at, request_status, result_state, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (aid, campaign_id, clip_id, render_id, platform, idempotency_key,
             number, now, request_status, "UNKNOWN_EXTERNAL_RESULT", now))
        await conn.commit()
        return {"submission_attempt_id": aid, "attempt_number": number}
    finally:
        await conn.close()


async def finish_submission_attempt(submission_attempt_id: str, *,
                                    request_status: str,
                                    provider_submission_id=None,
                                    provider_response_status=None,
                                    provider_response_metadata=None,
                                    result_state: str,
                                    error_code=None,
                                    error_message=None) -> None:
    from automation.submissions import redact_metadata
    conn = await database._get_connection()
    try:
        await conn.execute(
            "UPDATE automation_submission_attempts SET finished_at=?,"
            " request_status=?, provider_submission_id=?,"
            " provider_response_status=?, provider_response_metadata=?,"
            " result_state=?, error_code=?, error_message=? WHERE submission_attempt_id=?",
            (_now(), request_status, provider_submission_id,
             provider_response_status,
             json.dumps(redact_metadata(provider_response_metadata)),
             result_state, error_code,
             (str(error_message)[:500] if error_message else None),
             submission_attempt_id))
        await conn.commit()
    finally:
        await conn.close()


async def list_submission_attempts(idempotency_key: str) -> list[dict]:

    conn = await database._get_connection()
    try:
        cur = await conn.execute(
            "SELECT * FROM automation_submission_attempts WHERE idempotency_key=?"
            " ORDER BY attempt_number",
            (idempotency_key,))
        rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["provider_response_metadata"] = json.loads(
                    d.get("provider_response_metadata") or "{}")
            except Exception:
                d["provider_response_metadata"] = {}
            out.append(d)
        return out
    finally:
        await conn.close()
