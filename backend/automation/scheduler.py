"""
scheduler.py — Automation scheduler with resumable steps and idempotency (Phase 13).

  Campaign scan → eligibility → content processing → clip generation →
  rendering → approval → publishing → analytics → weekly report

Steps are resumable units recorded in automation_jobs with attempt counts,
heartbeats, and leases. Retries use exponential backoff and are bounded.
A crashed worker's lease expires (stale-job detection); the next run may
resume, retry, fail, or escalate to review — but NEVER blindly reruns
publishing (publishing stays idempotent via claim slots).

Idempotency key concept (preserved): campaign|clip|render|platform,
canonicalized in publishing.build_idem_key.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

JOB_STATES = {"queued", "running", "completed", "failed", "needs_review",
              "cancelled", "approved", "published"}

LEASE_TTL_S = float(os.getenv("SCHEDULER_LEASE_TTL_S", "300"))
HEARTBEAT_INTERVAL_S = float(os.getenv("SCHEDULER_HEARTBEAT_S", "15"))


def idempotency_key(campaign_id: str, source_clip_hash: str,
                    render_hash: str, platform: str) -> str:
    raw = f"{campaign_id}|{source_clip_hash}|{render_hash}|{platform}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def automation_mode() -> str:
    return os.getenv("AUTOMATION_MODE", "dry_run").lower()


def live_publishing_enabled() -> bool:
    return os.getenv("LIVE_PUBLISHING_ENABLED", "false").lower() == "true"


def human_approval_required() -> bool:
    return os.getenv("HUMAN_APPROVAL_REQUIRED", "true").lower() == "true"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class AutomationScheduler:
    """In-process scheduler; steps run as awaitables with hardened retries."""

    def __init__(self, max_retries: int = 2, backoff_base_s: float = 2.0):
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self._running: dict[str, dict] = {}
        self._hb_tasks: dict[str, asyncio.Task] = {}
        self.owner_id = f"worker_{uuid.uuid4().hex[:8]}"

    async def _heartbeat_loop(self, job_id: str, kind: str, store) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                try:
                    await store.heartbeat_job(job_id, self.owner_id, kind)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    def _stop_heartbeat(self, job_id: str) -> None:
        task = self._hb_tasks.pop(job_id, None)
        if task:
            task.cancel()

    async def run_step(self, job_id: str, kind: str, coro_factory,
                       campaign_id=None, clip_id=None, store=None,
                       failure_code_map=None) -> dict:
        """Run one resumable step: lease + heartbeat + bounded exponential
        backoff + failure isolation. Publishing callers must additionally
        hold an idempotent claim slot (never blindly rerun publishing)."""
        if store:
            await store.upsert_job(job_id, kind, "running", campaign_id, clip_id)
            await store.heartbeat_job(job_id, self.owner_id, kind)
            try:
                self._hb_tasks[job_id] = asyncio.get_running_loop().create_task(
                    self._heartbeat_loop(job_id, kind, store))
            except RuntimeError:
                pass
        self._running[job_id] = {"status": "running", "kind": kind, "attempt": 0}
        attempt, last_err, failure_code = 0, None, "unknown"
        while attempt <= self.max_retries:
            self._running[job_id]["attempt"] = attempt
            try:
                result = await coro_factory()
                self._stop_heartbeat(job_id)
                if store:
                    await store.upsert_job(job_id, kind, "completed", campaign_id,
                                           clip_id, payload={"result": str(result)[:500]})
                self._running[job_id] = {"status": "completed", "kind": kind,
                                         "result": result, "attempt": attempt}
                return {"job_id": job_id, "status": "completed", "result": result,
                        "attempt": attempt}
            except asyncio.CancelledError:
                self._stop_heartbeat(job_id)
                if store:
                    await store.upsert_job(job_id, kind, "cancelled", campaign_id,
                                           clip_id, error="cancelled",
                                           payload={"failure_code": "cancelled"})
                self._running[job_id] = {"status": "cancelled", "kind": kind}
                raise
            except Exception as exc:
                last_err = str(exc)
                failure_code = (failure_code_map or {}).get(type(exc).__name__, "step_failed")
                attempt += 1
                logger.warning("scheduler step %s attempt %d failed: %s", kind, attempt, exc)
                if attempt <= self.max_retries:
                    await asyncio.sleep(self.backoff_base_s * (2 ** (attempt - 1)))
        self._stop_heartbeat(job_id)
        if store:
            await store.upsert_job(job_id, kind, "failed", campaign_id, clip_id,
                                   error=last_err,
                                   payload={"failure_code": failure_code,
                                            "attempts": attempt})
        self._running[job_id] = {"status": "failed", "kind": kind,
                                 "error": last_err, "failure_code": failure_code}
        return {"job_id": job_id, "status": "failed", "error": last_err,
                "failure_code": failure_code}

    @staticmethod
    def recover_interrupted(jobs: list[dict], lease_ttl_s: float = LEASE_TTL_S,
                            now: float | None = None) -> list[dict]:
        """
        Decide fate of jobs found running/queued at scheduler (re)start.
        Publishing jobs are NEVER auto-rerun: they go to needs_review.
        Returns [{job_id, action}] where action ∈ resume|retry|mark_failed|require_review.
        """
        now = now if now is not None else time.time()
        out = []
        for j in jobs:
            status = j.get("status")
            if status not in ("running", "queued", "needs_review"):
                continue
            hb = j.get("heartbeat_at")
            try:
                age = now - datetime.datetime.fromisoformat(
                    str(hb).replace("Z", "+00:00")).timestamp() if hb else 1e9
            except Exception:
                age = 1e9
            stale = age > lease_ttl_s
            kind = str(j.get("kind", ""))
            payload = j.get("payload_json") or j.get("payload") or ""
            mentions_unknown = "UNKNOWN_EXTERNAL_RESULT" in str(payload)
            if "publish" in kind or mentions_unknown:
                out.append({"job_id": j["job_id"], "action": "require_review",
                            "reason": "publishing/ambiguous submissions never auto-rerun"})
            elif not stale and status == "running":
                out.append({"job_id": j["job_id"], "action": "resume",
                            "reason": "lease still valid"})
            elif status == "queued" or int(j.get("attempt", 0)) == 0:
                out.append({"job_id": j["job_id"], "action": "retry",
                            "reason": "never attempted or queued"})
            elif stale:
                out.append({"job_id": j["job_id"], "action": "mark_failed",
                            "reason": "stale lease, attempts exhausted"})
            else:
                out.append({"job_id": j["job_id"], "action": "require_review",
                            "reason": "ambiguous state"})
        return out

    @staticmethod
    def new_job_id(prefix: str = "job") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:10]}"
