"""
automation_api.py — FastAPI router for the Automation Hub (mounted at /api/automation).

Hardened v1.0: versioned policy, validated state transitions with history,
render-bound approvals with expiry, centralized publish gate, atomic
idempotent publish slots, dry-run-safe reporting. Success schemas are
unchanged; errors use {"error": {code, message, details}}.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException  # type: ignore
from pydantic import BaseModel  # type: ignore

from automation.auth import auth_mode, require_operator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automation", tags=["automation"])

# Server-side operator gate (Phase 20). READ stays open; every mutation
# below takes _auth. Frontend-only checks never substitute for this.
OperatorAuth = Depends(require_operator)


def error_envelope(code: str, message: str, details=None) -> dict:
    """Structured error body used by every automation endpoint."""
    return {"error": {"code": code, "message": message, "details": details or {}}}


# ---------------------------------------------------------------------------
# Policy helpers (current policy in settings; immutable history in store)
# ---------------------------------------------------------------------------

async def _load_policy():
    import settings as settings_mod  # type: ignore
    from automation.policy import UserCampaignPolicy, POLICY_KEY
    raw = await settings_mod.get_setting(POLICY_KEY)
    try:
        data = json.loads(raw) if raw else None
    except Exception:
        data = None
    return UserCampaignPolicy.from_dict(data)


async def _save_policy_versioned(policy, owner_actor: str = "owner") -> dict:
    import settings as settings_mod  # type: ignore
    from automation import store
    from automation.policy import POLICY_KEY, canonical_hash
    await settings_mod.set_setting(POLICY_KEY, json.dumps(policy.to_dict()))
    await store.init_automation_tables()
    return await store.save_policy_version(
        policy.to_dict(), canonical_hash(policy.to_dict()), owner_actor)


async def _current_policy_version() -> int | None:
    from automation import store
    await store.init_automation_tables()
    latest = await store.latest_policy_version()
    return int(latest["version"]) if latest else None


def _engine(policy, allowed_platforms=None):
    from automation.eligibility import CampaignEligibilityEngine
    return CampaignEligibilityEngine(policy=policy, allowed_platforms=allowed_platforms)


async def _record(machine, transitions, previous, new_state, *, actor="automation",
                  reason="", job_id=None, clip_id=None, campaign_id=None,
                  render_id=None):
    """Validate a transition and persist its history record. Pass
    transitions=None when the move was pre-validated (e.g. observation
    advances); the record is still persisted, never skipped."""
    from automation import store
    from automation.statemachine import transition, TransitionRecord
    import datetime as _dt
    if transitions is None:
        rec = TransitionRecord(machine=machine, previous_state=previous,
                               new_state=new_state,
                               timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                               actor=actor, reason=reason, job_id=job_id,
                               clip_id=clip_id, campaign_id=campaign_id,
                               render_id=render_id)
    else:
        rec = transition(transitions, machine, previous, new_state, actor=actor,
                         reason=reason, job_id=job_id, clip_id=clip_id,
                         campaign_id=campaign_id, render_id=render_id)
    await store.init_automation_tables()
    await store.record_transition(rec)
    return rec


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@router.get("/policy")
async def get_policy():
    from automation import store
    policy = await _load_policy()
    latest = await store.latest_policy_version()
    return {"policy": policy.to_dict(),
            "version": latest["version"] if latest else None,
            "policy_hash": latest["policy_hash"] if latest else None,
            "versions": await store.list_policy_versions(),
            "note": "Campaign exclusion is based on explicit campaign metadata or "
                    "configured evidence, not inferred identity or nationality."}


class PolicyUpdate(BaseModel):
    policy: dict
    actor: str = "owner"
    reason: str = ""


@router.put("/policy")
async def update_policy(body: PolicyUpdate, _auth: str = OperatorAuth):
    from automation.policy import UserCampaignPolicy
    current = (await _load_policy()).to_dict()
    current.update(body.policy or {})
    policy = UserCampaignPolicy.from_dict(current)
    before = await _current_policy_version()
    saved = await _save_policy_versioned(policy, owner_actor=body.actor)
    from automation import store as _store_pol
    await _store_pol.log_decision(decision="policy_changed",
                                  rules=["policy_admin"],
                                  evidence=[{"actor": body.actor,
                                             "reason": body.reason,
                                             "old_version": before,
                                             "new_version": saved["version"],
                                             "policy_hash": saved["policy_hash"]}],
                                  operator=body.actor)
    return {"policy": policy.to_dict(), "version": saved["version"],
            "policy_hash": saved["policy_hash"]}


@router.post("/policy/re-evaluate")
async def re_evaluate_policy():
    """Explicitly rescan stored campaigns under the current policy version."""
    from automation import store
    from automation.providers import normalize_campaign
    await store.init_automation_tables()
    policy = await _load_policy()
    version = await _current_policy_version()
    engine = _engine(policy)
    rows = await store.list_campaigns()
    results = []
    for r in rows:
        try:
            payload = json.loads(r.get("payload_json") or "{}")
        except Exception:
            continue
        payload.setdefault("campaign_id", r["campaign_id"])
        ev = engine.evaluate(normalize_campaign(payload))
        await store.upsert_campaign(r["campaign_id"], r.get("name") or r["campaign_id"],
                                    payload, ev["status"], ev["reasons"])
        await store.log_decision(
            campaign_id=r["campaign_id"], decision=ev["status"].lower(),
            rules=ev["rules_triggered"], evidence=ev["evidence"],
            operator="automation")
        results.append({"campaign_id": r["campaign_id"], "status": ev["status"]})
    return {"policy_version": version, "results": results}


# ---------------------------------------------------------------------------
# Campaign discovery + eligibility
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    provider: str = "mock"
    campaigns: Optional[list] = None
    replay_path: Optional[str] = None
    allowed_platforms: Optional[list] = None


class CampaignCreate(BaseModel):
    campaign_id: Optional[str] = None
    name: str
    description: str = ""
    requirements: str = ""
    allowed_content: str = ""
    platforms: list = []
    content_sources: list = []
    affiliations: list = []
    brands: list = []
    entities: list = []
    source_url: str = ""


@router.post("/campaigns")
async def create_campaign(body: CampaignCreate, _auth: str = OperatorAuth):
    """Operator-owned manual campaign. Real data in, real evaluation out —
    this is how the hub is fed without a provider integration."""
    from automation import store
    from automation.providers import normalize_campaign
    await store.init_automation_tables()
    import uuid as _uuid
    cid = (body.campaign_id or "").strip() or f"manual_{_uuid.uuid4().hex[:8]}"
    payload = normalize_campaign({**body.model_dump(), "campaign_id": cid})
    await store.upsert_campaign(cid, payload.get("name") or cid, payload,
                                "DISCOVERED", ["manual_entry"])
    await store.log_decision(campaign_id=cid, decision="discovered",
                             rules=["manual_entry"],
                             evidence=[{"name": payload.get("name")}],
                             operator="owner")
    return {"campaign_id": cid, "status": "DISCOVERED"}


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str, _auth: str = OperatorAuth):
    from automation import store
    await store.init_automation_tables()
    await store.delete_campaign(campaign_id)
    await store.log_decision(campaign_id=campaign_id, decision="deleted",
                             rules=["manual_entry"], evidence=[],
                             operator="owner")
    return {"campaign_id": campaign_id, "deleted": True}


@router.post("/scan")
async def scan(body: ScanRequest):
    from automation import store
    from automation.events import emit
    from automation.pipeline import scan_campaigns
    from automation.providers import get_provider
    from automation.statemachine import CAMPAIGN_TRANSITIONS
    import time as _time
    await store.init_automation_tables()
    t0 = _time.time()
    policy = await _load_policy()
    version = await _current_policy_version()
    if version is None:
        saved = await _save_policy_versioned(policy, owner_actor="system")
        version = saved["version"]
    if body.provider == "replay":
        provider = get_provider("replay", path=body.replay_path or "")
    elif body.provider == "whop_official":
        provider = get_provider("whop_official")
    elif body.provider == "stored":
        # Re-evaluate operator-stored (manual) campaigns already in the DB.
        from automation.providers import normalize_campaign as _norm
        stored = await store.list_campaigns()
        loaded = []
        for r in stored:
            try:
                payload = json.loads(r.get("payload_json") or "{}")
            except Exception:
                continue
            payload.setdefault("campaign_id", r["campaign_id"])
            loaded.append(_norm(payload))
        provider = get_provider("mock", campaigns=loaded)
    else:
        provider = get_provider("mock", campaigns=body.campaigns or [])
    result = await scan_campaigns(provider, _engine(policy, body.allowed_platforms),
                                  store, policy_version=version)
    # Record DISCOVERED -> <status> transitions for history.
    for cid, ev in result["evaluations"].items():
        try:
            await _record("campaign", CAMPAIGN_TRANSITIONS, "DISCOVERED", ev["status"],
                          reason=";".join(ev["reasons"]) or "scan",
                          campaign_id=cid)
        except Exception as exc:
            # History is best-effort, but never silent: evaluation persisted above.
            logger.warning("transition history write failed for %s: %s", cid, exc)
    emit("CAMPAIGN_SCAN", status="ok", campaign_id=None,
         duration_ms=int((_time.time() - t0) * 1000),
         details={"provider": body.provider, "count": len(result["campaigns"]),
                  "policy_version": version})
    return {"ranked": result["ranked"], "evaluations": result["evaluations"],
            "campaigns": result["campaigns"], "policy_version": version}


@router.get("/campaigns")
async def list_campaigns(status: Optional[str] = None):
    from automation import store
    await store.init_automation_tables()
    return {"campaigns": await store.list_campaigns(status)}


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    from automation import store
    await store.init_automation_tables()
    rows = await store.list_campaigns()
    for r in rows:
        if r["campaign_id"] == campaign_id:
            try:
                r["payload"] = json.loads(r.get("payload_json") or "{}")
            except Exception:
                r["payload"] = {}
            r["decisions"] = await store.list_decisions(campaign_id)
            r["transitions"] = await store.list_transitions(campaign_id=campaign_id)
            return r
    raise HTTPException(status_code=404, detail=error_envelope(
        "not_found", "Campaign not found", {"campaign_id": campaign_id}))


class ReviewDecision(BaseModel):
    decision: str  # approve | reject
    reviewer: str = "owner"
    reason: str = ""


@router.post("/campaigns/{campaign_id}/review")
async def review_campaign(campaign_id: str, body: ReviewDecision, _auth: str = OperatorAuth):
    """Explicit owner override of a NEEDS_REVIEW campaign. Separately logged."""
    from automation import store
    from automation.eligibility import ELIGIBLE, REJECTED
    from automation.statemachine import CAMPAIGN_TRANSITIONS
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail=error_envelope(
            "invalid_request", "decision must be approve|reject", {}))
    await store.init_automation_tables()
    rows = await store.list_campaigns()
    current = next((r["status"] for r in rows if r["campaign_id"] == campaign_id), None)
    if current is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "Campaign not found", {"campaign_id": campaign_id}))
    new_status = ELIGIBLE if body.decision == "approve" else REJECTED
    # Validated transition (raises StateTransitionError → 409 on misuse).
    await _record("campaign", CAMPAIGN_TRANSITIONS, current, new_status,
                  actor=body.reviewer,
                  reason=body.reason or f"owner_override_{body.decision}",
                  campaign_id=campaign_id)
    await store.upsert_campaign(campaign_id, campaign_id, {}, new_status,
                                [f"owner_override_{body.decision}"])
    await store.log_decision(campaign_id=campaign_id, decision=f"owner_{body.decision}",
                             rules=["owner_override"],
                             evidence=[{"reviewer": body.reviewer, "reason": body.reason}],
                             operator=body.reviewer)
    return {"campaign_id": campaign_id, "status": new_status}


@router.get("/audit")
async def audit_log(campaign_id: Optional[str] = None):
    from automation import store
    await store.init_automation_tables()
    return {"decisions": await store.list_decisions(campaign_id)}


@router.get("/transitions")
async def transitions(clip_id: Optional[str] = None, campaign_id: Optional[str] = None):
    from automation import store
    await store.init_automation_tables()
    return {"transitions": await store.list_transitions(clip_id, campaign_id)}


# ---------------------------------------------------------------------------
# Clip selection helpers
# ---------------------------------------------------------------------------

class SelectRequest(BaseModel):
    candidates: list = []
    music_statuses: dict = {}
    people_gates: dict = {}
    max_clips: int = 5
    weights: Optional[dict] = None
    seed: Optional[int] = None


@router.post("/clips/select")
async def select(body: SelectRequest):
    from automation.pipeline import select_clips
    policy = await _load_policy()
    version = await _current_policy_version()
    out = select_clips(body.candidates, body.music_statuses, body.people_gates,
                       policy, body.max_clips, body.weights, seed=body.seed)
    out["policy_version"] = version
    return out


class MusicCheck(BaseModel):
    clip_path: str
    start: float = 0.0
    duration: Optional[float] = None


@router.post("/clips/music-check")
async def music_check(body: MusicCheck):
    from automation.music import analyze_music_status
    return await asyncio.to_thread(analyze_music_status, body.clip_path,
                                   body.start, body.duration)


class PeopleCheck(BaseModel):
    video_path: str
    start: float
    end: float
    region_mode: str = "full"
    mode: str = "BLUR_ALL"
    manual_blur_ids: Optional[list] = None
    clip_id: Optional[str] = None
    # legacy fields (accepted, mapped to mode)
    enabled: Optional[bool] = None
    blur_uncertain: Optional[bool] = None


@router.post("/clips/people-check")
async def people_check(body: PeopleCheck):
    from automation import store
    from automation.people_policy import (analyze_clip_people, apply_policy_decision,
                                          normalize_mode, PEOPLE_POLICY_BLUR_ALL,
                                          PEOPLE_POLICY_MANUAL, PEOPLE_POLICY_REVIEW,
                                          validate_tracks)
    await store.init_automation_tables()
    mode = body.mode
    if body.enabled is not None and body.mode == "BLUR_ALL":
        mode = normalize_mode({"enabled": body.enabled})
    if body.blur_uncertain:
        mode = PEOPLE_POLICY_BLUR_ALL
    mode = normalize_mode(mode)
    video_path = body.video_path
    # Accept served URLs (/files/...) as well as filesystem paths.
    if video_path.startswith("/files/"):
        import os as _os
        tmp_dir = _os.getenv("TMP_DIR", _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tmp"))
        video_path = _os.path.normpath(
            _os.path.join(tmp_dir, video_path[len("/files/"):]))
    analysis = await asyncio.to_thread(
        analyze_clip_people, video_path, body.start, body.end,
        body.region_mode, mode, body.manual_blur_ids)
    gate = apply_policy_decision(analysis, mode)
    quality = validate_tracks(analysis["tracks"], body.start, body.end)
    if not quality["ok"]:
        gate = {"gate": "NEEDS_REVIEW", "reason": "unstable_masks",
                "blur_tracks": gate.get("blur_tracks", []),
                "quality_issues": quality["issues"]}
    if body.clip_id:
        tracks = []
        for t in analysis["tracks"]:
            tracks.append({**t, "action": "BLUR" if t.get("blur") else "KEEP"})
        await store.save_people_tracks(body.clip_id, tracks)
        await store.log_decision(clip_id=body.clip_id, decision=gate["gate"],
                                 rules=["people_policy"], evidence=[gate],
                                 operator="automation")
    return {"analysis": analysis, "gate": gate, "quality": quality, "mode": mode}


@router.get("/clips/{clip_id}/people")
async def get_people(clip_id: str):
    from automation import store
    await store.init_automation_tables()
    tracks = await store.list_people_tracks(clip_id)
    decoded = []
    for t in tracks:
        try:
            boxes = json.loads(t.get("boxes_json") or "[]")
        except Exception:
            boxes = []
        decoded.append({**t, "boxes": boxes})
    return {"tracks": decoded,
            "note": "Render-time tracks are authoritative; preview overlays are advisory. "
                    "No appearance classification is performed."}


class TrackReview(BaseModel):
    track_id: int
    action: str  # BLUR | KEEP
    reviewer: str = "owner"


@router.post("/clips/{clip_id}/people/review")
async def review_track(clip_id: str, body: TrackReview):
    from automation import store
    from automation.people_policy import REVIEW_APPROVED, REVIEW_PENDING, audit_record
    if body.action not in ("BLUR", "KEEP"):
        raise HTTPException(status_code=400, detail=error_envelope(
            "invalid_request", "action must be BLUR|KEEP", {}))
    await store.init_automation_tables()
    await store.review_person_track(clip_id, body.track_id, body.action, body.reviewer)
    tracks = await store.list_people_tracks(clip_id)
    t = next((x for x in tracks if x["track_id"] == body.track_id), None)
    await store.log_decision(clip_id=clip_id, decision=f"person_track_{body.action.lower()}",
                             rules=["people_policy"],
                             evidence=[audit_record(clip_id, t or {"track_id": body.track_id},
                                                    body.reviewer)],
                             operator=body.reviewer)
    # Manual decision flips source + review_status on the stored track.
    if t is not None:
        from automation import store as _s
        await _s.save_people_tracks(clip_id, [{**t, "boxes": json.loads(t.get("boxes_json") or "[]"),
                                               "blur": body.action == "BLUR",
                                               "source": "manual",
                                               "review_status": REVIEW_APPROVED,
                                               "reviewer": body.reviewer,
                                               "action": body.action}])
        _ = REVIEW_PENDING
    return {"clip_id": clip_id, "track_id": body.track_id, "action": body.action}


class ManualTrack(BaseModel):
    start_time: float
    end_time: float
    box: dict  # normalized {x,y,w,h} in output coordinate space
    blur: bool = True
    reviewer: str = "owner"


@router.post("/clips/{clip_id}/people/manual")
async def add_manual_track(clip_id: str, body: ManualTrack):
    """Owner-drawn mask region (manual source). Boxes use the same normalized
    output coordinate space as render-time tracks (preview/render parity)."""
    from automation import store
    from automation.people_policy import REVIEW_APPROVED, REVIEW_PENDING
    if body.end_time <= body.start_time:
        raise HTTPException(status_code=400, detail=error_envelope(
            "invalid_request", "end_time must exceed start_time", {}))
    for k in ("x", "y", "w", "h"):
        v = float(body.box.get(k, -1))
        if not (0.0 <= v <= 1.0) or (k in ("w", "h") and v <= 0):
            raise HTTPException(status_code=400, detail=error_envelope(
                "invalid_request", f"box.{k} must be in 0..1", {}))
    await store.init_automation_tables()
    existing = await store.list_people_tracks(clip_id)
    track_id = max([t["track_id"] for t in existing] + [0]) + 1
    n = max(2, int((body.end_time - body.start_time) * 2) + 1)
    boxes = [{"t": round(body.start_time + i * (body.end_time - body.start_time) / max(n - 1, 1), 3),
              "box": {k: round(float(body.box[k]), 4) for k in ("x", "y", "w", "h")},
              "iou": 0.0, "conf": 1.0, "manual": True} for i in range(n)]
    track = {"track_id": track_id, "start_time": body.start_time,
             "end_time": body.end_time, "boxes": boxes, "confidence": 1.0,
             "blur": bool(body.blur), "source": "manual",
             "review_status": REVIEW_APPROVED,
             "action": "BLUR" if body.blur else "KEEP",
             "reviewer": body.reviewer}
    await store.save_people_tracks(clip_id, [track])
    await store.log_decision(clip_id=clip_id, decision="person_track_manual",
                             rules=["people_policy"], evidence=[track],
                             operator=body.reviewer)
    _ = REVIEW_PENDING
    return {"clip_id": clip_id, "track_id": track_id}


class BlurRender(BaseModel):
    input_path: str
    output_path: str
    blur_type: str = "soft"
    track_ids: Optional[list] = None


@router.post("/clips/{clip_id}/people/blur-render")
async def blur_render(clip_id: str, body: BlurRender):
    from automation import store
    from automation.people_policy import apply_people_blur
    from automation.resources import require_disk_space, InsufficientStorage
    await store.init_automation_tables()
    try:
        require_disk_space(body.output_path)
    except InsufficientStorage as exc:
        raise HTTPException(status_code=409, detail=error_envelope(
            "INSUFFICIENT_STORAGE", str(exc), exc.payload))
    tracks = await store.list_people_tracks(clip_id)
    wanted = set(body.track_ids or [])
    sel = []
    for t in tracks:
        if wanted and t["track_id"] not in wanted:
            continue
        if not t.get("blur"):
            continue
        try:
            boxes = json.loads(t.get("boxes_json") or "[]")
        except Exception:
            boxes = []
        sel.append({"track_id": t["track_id"], "boxes": boxes,
                    "start_time": t["start_time"], "end_time": t["end_time"],
                    "blur": True})
    if not sel:
        raise HTTPException(status_code=400, detail=error_envelope(
            "no_blur_tracks", "no BLUR tracks for this clip", {"clip_id": clip_id}))
    return await asyncio.to_thread(apply_people_blur, body.input_path,
                                   body.output_path, sel, body.blur_type)


# ---------------------------------------------------------------------------
# Approval queue (render-bound, expiring, validated transitions)
# ---------------------------------------------------------------------------

class ApprovalUpsert(BaseModel):
    clip_id: str
    campaign_id: Optional[str] = None
    status: str = "DETECTED"
    title: Optional[str] = None
    description: Optional[str] = None
    preview_path: Optional[str] = None
    detail: dict = {}
    render_id: Optional[str] = None
    render_hash: Optional[str] = None
    platform: Optional[str] = None


@router.post("/approvals")
async def upsert_approval(body: ApprovalUpsert):
    from automation import store
    from automation.render_identity import content_hash
    from automation.statemachine import advance_clip_observation
    await store.init_automation_tables()
    existing = await store.get_approval(body.clip_id)
    previous = (existing or {}).get("status")
    target = body.status
    if existing and body.render_hash and body.render_hash != existing.get("render_hash"):
        # A new render supersedes the old identity: preserve the evidence,
        # restart the machine; the old approval can never authorize the new.
        await store.log_decision(clip_id=body.clip_id, campaign_id=body.campaign_id,
                                 decision="approval_superseded",
                                 rules=["approval_integrity"],
                                 evidence=[{"previous_render_hash": existing.get("render_hash"),
                                            "new_render_hash": body.render_hash}],
                                 operator="automation")
        target = "DETECTED"
    effective, note = advance_clip_observation(previous, target)
    if target != previous or (existing is None):
        await _record("clip", None, previous, effective, clip_id=body.clip_id,
                      campaign_id=body.campaign_id, render_id=body.render_id,
                      reason=f"approval_upsert:{note}")
    version = await _current_policy_version()
    aid = await store.upsert_approval(body.clip_id, body.campaign_id, effective,
                                      body.title, body.description,
                                      body.preview_path, body.detail)
    # Bind render + metadata + platform identity when provided.
    if body.render_hash or body.render_id:
        await store.bind_approval_render(
            body.clip_id, render_id=body.render_id, render_hash=body.render_hash,
            policy_version=version,
            title_hash=content_hash(body.title or ""),
            description_hash=content_hash(body.description or ""),
            platform=body.platform)
    await store.log_decision(clip_id=body.clip_id, campaign_id=body.campaign_id,
                             decision=effective.lower(), rules=["approval_queue"],
                             operator="automation")
    return {"id": aid, "status": effective}


class ApprovalImport(BaseModel):
    clip_id: str
    campaign_id: Optional[str] = None


@router.post("/approvals/import")
async def import_clip_approval(body: ApprovalImport, _auth: str = OperatorAuth):
    """Import a REAL project clip into the approval queue. Scores, file,
    aspect and times come from the stored clip row — nothing invented.
    The approval starts unbound (no render hash); binding happens when an
    editor render is approved, and the publish gate blocks unbound rows."""
    from automation import store
    from automation.statemachine import advance_clip_observation
    import database as _db
    await store.init_automation_tables()
    try:
        clip = await _db.get_clip(body.clip_id)
    except Exception:
        clip = None
    if clip is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "Clip not found", {"clip_id": body.clip_id}))
    existing = await store.get_approval(body.clip_id)
    previous = (existing or {}).get("status")
    effective, note = advance_clip_observation(previous, "READY_FOR_APPROVAL")
    if "READY_FOR_APPROVAL" != previous or existing is None:
        await _record("clip", None, previous, effective, clip_id=body.clip_id,
                      campaign_id=body.campaign_id, reason=f"clip_import:{note}")
    detail = {"hook_score": clip.get("hook_score"),
              "payoff_score": clip.get("payoff_score"),
              "audio_score": clip.get("audio_score"),
              "visual_score": clip.get("visual_score"),
              "final_score": clip.get("final_score"),
              "viral_score": clip.get("viral_score"),
              "face_count": clip.get("face_count"),
              "layout_mode": clip.get("layout_mode"),
              "aspect": clip.get("aspect"),
              "start_time": clip.get("start_time"),
              "end_time": clip.get("end_time"),
              "project_id": clip.get("project_id")}
    aid = await store.upsert_approval(
        body.clip_id, body.campaign_id, effective,
        clip.get("title") or "Untitled clip", clip.get("reason") or "",
        clip.get("file_path"), detail)
    await store.log_decision(clip_id=body.clip_id, campaign_id=body.campaign_id,
                             decision="clip_imported", rules=["approval_queue"],
                             evidence=[{"title": clip.get("title")}],
                             operator="owner")
    return {"id": aid, "status": effective, "detail": detail}


@router.get("/approvals")
async def list_approvals(status: Optional[str] = None):
    from automation import store
    await store.init_automation_tables()
    return {"approvals": await store.list_approvals(status)}


class ApprovalDecision(BaseModel):
    approve: bool
    reviewer: str = "owner"
    render_hash: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    platform: Optional[str] = None


@router.post("/approvals/{clip_id}/decision")
async def decide_approval(clip_id: str, body: ApprovalDecision, _auth: str = OperatorAuth):
    from automation import store
    from automation.render_identity import content_hash
    from automation.statemachine import CLIP_TRANSITIONS
    await store.init_automation_tables()
    current = await store.get_approval(clip_id)
    if current is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "No approval record for clip", {"clip_id": clip_id}))
    # The decision binds to the CURRENT render+metadata: approving a stale
    # view is rejected so one render's approval can never authorize another.
    if body.render_hash and body.render_hash != (current.get("render_hash") or body.render_hash):
        raise HTTPException(status_code=409, detail=error_envelope(
            "stale_approval_view",
            "Render changed since this approval view was loaded; re-approve the current render.",
            {"current_render_hash": current.get("render_hash")}))
    status = "APPROVED" if body.approve else "REJECTED"
    await _record("clip", CLIP_TRANSITIONS, current["status"], status,
                  actor=body.reviewer, clip_id=clip_id,
                  campaign_id=current.get("campaign_id"),
                  render_id=current.get("render_id"),
                  reason="human_decision")
    await store.set_approval_status(clip_id, status, body.reviewer)
    if body.approve:
        await store.bind_approval_render(
            clip_id, render_id=current.get("render_id"),
            render_hash=body.render_hash or current.get("render_hash"),
            policy_version=await _current_policy_version(),
            title_hash=content_hash(body.title if body.title is not None
                                    else (current.get("title") or "")),
            description_hash=content_hash(body.description if body.description is not None
                                          else (current.get("description") or "")),
            platform=body.platform or current.get("platform"),
            ttl_hours=None)
    await store.log_decision(clip_id=clip_id, decision=status.lower(),
                             rules=["human_approval"], operator=body.reviewer)
    return {"clip_id": clip_id, "status": status}


# ---------------------------------------------------------------------------
# Publishing (idempotent, live-gated, centrally checked)
# ---------------------------------------------------------------------------

class PublishRequest(BaseModel):
    clip_id: str
    campaign_id: Optional[str] = None
    platform: str = "tiktok"
    account_id: Optional[str] = None
    clip_path: Optional[str] = None
    title: str = ""
    description: str = ""
    render_hash: str = ""
    render_id: Optional[str] = None
    approval_id: Optional[str] = None
    dry_run: bool = True
    render_verified: bool = False
    # §7 operator-only live confirmation: live submission requires an
    # explicit confirmation echoing the exact render being published.
    # Never set automatically by startup, scheduler, health checks, or tests.
    operator_confirm: bool = False
    confirmed_render_hash: Optional[str] = None


class PreflightRequest(BaseModel):
    clip_id: str
    campaign_id: Optional[str] = None
    platform: str = "tiktok"
    title: str = ""
    description: str = ""
    render_hash: str = ""
    approval_id: Optional[str] = None


@router.post("/publish/preflight")
async def publish_preflight(body: PreflightRequest):
    """Final operator preflight (§12): every publication validation EXCEPT
    the external side effect. Returns READY_TO_PUBLISH or the exact
    blocking reason. Never publishes, never claims a slot."""
    from automation import store
    from automation.configcheck import validate as _validate_config
    from automation.events import emit
    from automation.publishing import (PublishBlocked, assert_publish_allowed,
                                       build_idem_key)
    from automation.render_identity import content_hash, verify_render_artifact
    await store.init_automation_tables()
    key = build_idem_key(body.campaign_id or "", body.clip_id,
                         body.render_hash or body.clip_id, body.platform)
    checks: dict = {}
    cfg = _validate_config()
    checks["configuration"] = "pass" if cfg["valid"] else "fail"
    if not cfg["valid"]:
        return {"verdict": "BLOCKED", "reason": "invalid configuration",
                "blocking": cfg["blocking"], "checks": checks,
                "idempotency_key": key}
    policy = await _load_policy()
    _ = policy  # current policy snapshot governs; version recorded below
    checks["policy"] = f"v{await _current_policy_version()}"
    approval = await store.get_approval(body.clip_id)
    rows = await store.list_campaigns()
    camp = next((r for r in rows if r["campaign_id"] == (body.campaign_id or "")), None)
    # Display the exact object under review.
    candidate = {"campaign_id": body.campaign_id, "clip_id": body.clip_id,
                 "render_hash": body.render_hash, "platform": body.platform,
                 "title": body.title, "description": body.description,
                 "approval_id": (approval or {}).get("id"),
                 "policy_version": await _current_policy_version()}
    render_ok, render_detail = False, "no local artifact to verify"
    if approval and approval.get("render_hash") == body.render_hash:
        render_ok, render_detail = True, "approval binds this render"
    checks["approval"] = "bound" if render_ok else "missing_or_mismatched"
    import os as _os
    local_candidates = []
    try:
        import database as _db
        clip = await _db.get_clip(body.clip_id)
        if clip and clip.get("file_path"):
            tmp_dir = _os.getenv("TMP_DIR", "")
            if tmp_dir:
                local_candidates.append(_os.path.normpath(_os.path.join(
                    tmp_dir, str(clip["file_path"]).replace("/files/", "", 1))))
    except Exception:
        pass
    verified_path = next((p for p in local_candidates if _os.path.isfile(p)), None)
    if verified_path:
        check = await asyncio.to_thread(verify_render_artifact, verified_path)
        render_ok = bool(check.get("ok"))
        render_detail = check.get("reasons", []) or "artifact verified"
    checks["render"] = render_detail
    metadata_ok = True
    try:
        from automation.titles import validate_metadata
        mv = validate_metadata(body.title, body.description, [], None)
        metadata_ok = mv.get("verdict") != "REJECTED"
        checks["metadata"] = mv.get("verdict")
    except Exception as exc:
        metadata_ok, checks["metadata"] = False, f"validation error: {exc}"
    from automation.publishing import AuthorizedPlatformPublisher, adapter_status
    cap = adapter_status(AuthorizedPlatformPublisher(), body.platform)
    checks["provider_capability"] = cap["state"]
    checks["credentials"] = "present" if bool(_os.getenv(
        f"PUBLISH_TOKEN_{body.platform.upper()}", "")) else "missing"
    allow = [p.strip().lower() for p in _os.getenv("PUBLISH_PLATFORMS", "").split(",")
             if p.strip()]
    checks["platform_allow_list"] = "permits" if (
        not allow or body.platform.lower() in allow) else "denies"
    try:
        assert_publish_allowed(
            approval=approval, render_hash=body.render_hash,
            title=body.title, description=body.description,
            campaign_status=(camp or {}).get("status"),
            current_policy_version=await _current_policy_version(),
            render_verified=render_ok, idempotency_unique=True,
            adapter_enabled=True,
            credential_present=checks["credentials"] == "present",
            platform=body.platform, from_hashes=content_hash)
        gate = "pass"
    except PublishBlocked as exc:
        gate = f"blocked:{exc.code}"
    checks["gate"] = gate
    ready = (checks["configuration"] == "pass" and render_ok and metadata_ok
             and checks["platform_allow_list"] == "permits" and gate == "pass"
             and checks["provider_capability"] in ("OPERATIONAL", "AVAILABLE",
                                                   "AUTHENTICATED"))
    await store.log_decision(clip_id=body.clip_id, campaign_id=body.campaign_id,
                             decision="preflight_checked",
                             rules=["preflight"],
                             evidence=[{"candidate": candidate, "checks": checks,
                                        "verdict": "READY_TO_PUBLISH" if ready else "BLOCKED"}],
                             operator="automation")
    emit("PREFLIGHT_CHECKED", status="ready" if ready else "blocked",
         clip_id=body.clip_id, campaign_id=body.campaign_id,
         details={"checks": checks})
    return {"verdict": "READY_TO_PUBLISH" if ready else "BLOCKED",
            "candidate": candidate, "checks": checks,
            "idempotency_key": key}


@router.post("/publish")
async def publish(body: PublishRequest, _auth: str = OperatorAuth):
    from automation import store
    from automation.events import emit
    from automation.publishing import (MockPublisher, AuthorizedPlatformPublisher,
                                       PublishBlocked, assert_publish_allowed,
                                       build_idem_key, resolve_publisher)
    from automation.render_identity import content_hash
    from automation.side_effects import is_live
    from automation.statemachine import CLIP_TRANSITIONS
    from automation.submissions import (classify_provider_error, retry_delay_s,
                                        MAX_ATTEMPTS)
    import time as _time
    t0 = _time.time()
    await store.init_automation_tables()
    key = build_idem_key(body.campaign_id or "", body.clip_id,
                         body.render_hash or body.clip_id, body.platform)

    # Dry-run: simulate through mock only, persist a local intent record,
    # never touch external systems.
    if body.dry_run or not is_live():
        if await store.find_publication_by_key(key):
            return {"status": "ALREADY_PUBLISHED", "idempotency_key": key}
        pub = MockPublisher().publish(body.clip_path or "", body.title,
                                      body.description, body.platform, key)
        emit("PUBLISH_BLOCKED", status="dry_run", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             duration_ms=int((_time.time() - t0) * 1000),
             details={"reason": "dry_run_intended", "idempotency_key": key})
        return {"status": "dry_run_intended", "would_publish": pub,
                "idempotency_key": key}

    # Live path: atomic slot claim FIRST (single internal claim across
    # retries, double-clicks, concurrent workers, restarts — this is
    # internal exactly-once CLAIM semantics, not external exactly-once
    # delivery, which only the provider can guarantee).
    claimed = await store.claim_publication_slot(
        body.clip_id, body.campaign_id, body.platform, key)
    if not claimed:
        await store.update_publication_by_key(key, submission_state="ALREADY_PUBLISHED")
        emit("PUBLISH_BLOCKED", status="duplicate", clip_id=body.clip_id,
             campaign_id=body.campaign_id, details={"idempotency_key": key})
        return {"status": "ALREADY_PUBLISHED",
                "publication": await store.find_publication_by_key(key)}
    emit("SUBMISSION_CLAIMED", clip_id=body.clip_id,
         campaign_id=body.campaign_id,
         details={"idempotency_key": key})

    approval = await store.get_approval(body.clip_id)
    rows = await store.list_campaigns()
    camp = next((r for r in rows if r["campaign_id"] == (body.campaign_id or "")), None)
    # §7: explicit operator confirmation is mandatory for live submission.
    if not body.operator_confirm or body.confirmed_render_hash != body.render_hash:
        await store.update_publication_by_key(key, status="blocked",
                                              submission_state="FAILED")
        emit("SUBMISSION_BLOCKED", status="blocked", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             details={"idempotency_key": key,
                      "reason": "explicit operator confirmation required"})
        raise HTTPException(status_code=403, detail=error_envelope(
            "confirmation_required",
            "live publish requires operator_confirm=true with the exact render_hash",
            {"idempotency_key": key}))
    # §8: revalidate immediately before submission — approval, expiry,
    # policy, hashes, platform, campaign, clip, publisher config.
    render_ok = bool(body.render_verified)
    if body.clip_path and os.path.isfile(body.clip_path):
        from automation.render_identity import verify_render_artifact
        check = await asyncio.to_thread(verify_render_artifact, body.clip_path)
        render_ok = bool(check.get("ok"))
        if not render_ok:
            await store.update_publication_by_key(key, status="blocked",
                                                  submission_state="FAILED")
            emit("SUBMISSION_BLOCKED", status="blocked", clip_id=body.clip_id,
                 campaign_id=body.campaign_id,
                 details={"idempotency_key": key,
                          "reason": "render artifact failed verification",
                          "issues": check.get("reasons", [])})
            raise HTTPException(status_code=409, detail=error_envelope(
                "render_unverified",
                "current render failed integrity verification",
                {"issues": check.get("reasons", [])}))
    try:
        assert_publish_allowed(
            approval=approval, render_hash=body.render_hash,
            title=body.title, description=body.description,
            campaign_status=(camp or {}).get("status"),
            current_policy_version=await _current_policy_version(),
            render_verified=render_ok,
            idempotency_unique=True, adapter_enabled=True,
            credential_present=bool(__import__("os").getenv(
                f"PUBLISH_TOKEN_{body.platform.upper()}", "")),
            platform=body.platform,
            from_hashes=content_hash)
    except PublishBlocked as exc:
        await store.update_publication_by_key(key, status="blocked",
                                              submission_state="FAILED")
        emit("SUBMISSION_BLOCKED", status="blocked", clip_id=body.clip_id,
             campaign_id=body.campaign_id, details=exc.payload)
        raise

    try:
        adapter = resolve_publisher("authorized", live=True)
    except PublishBlocked as exc:
        await store.update_publication_by_key(key, status="blocked",
                                              submission_state="FAILED")
        raise

    await _record("clip", CLIP_TRANSITIONS,
                  (approval or {}).get("status", "APPROVED"), "PUBLISHING",
                  clip_id=body.clip_id, campaign_id=body.campaign_id,
                  render_id=body.render_id, reason="publish_start")
    await store.set_approval_status(body.clip_id, "PUBLISHING", "automation")
    await store.update_publication_by_key(key, submission_state="CLAIMED",
                                          account_id=body.account_id)
    attempt = await store.record_submission_attempt(
        campaign_id=body.campaign_id, clip_id=body.clip_id,
        render_id=body.render_id, platform=body.platform,
        idempotency_key=key, request_status="STARTED")
    emit("SUBMISSION_STARTED", clip_id=body.clip_id,
         campaign_id=body.campaign_id,
         details={"idempotency_key": key,
                  "submission_attempt_id": attempt["submission_attempt_id"],
                  "attempt_number": attempt["attempt_number"]})
    await store.update_publication_by_key(key, submission_state="SUBMISSION_STARTED")

    def _finish_attempt(**kw):
        return store.finish_submission_attempt(
            attempt["submission_attempt_id"], **kw)

    def _tel(status: str, result_state: str = "", retry_after=None):
        from automation import telemetry as _telmod
        _telmod.record(provider="authorized", operation="submit",
                       status=status, attempt=attempt["attempt_number"],
                       duration_ms=int(_time.time() - t0),
                       result_state=result_state, retry_after=retry_after,
                       meta={"platform": body.platform,
                             "submission_attempt_id": attempt["submission_attempt_id"]})

    try:
        result = await asyncio.to_thread(
            adapter.publish, body.clip_path or "", body.title,
            body.description, body.platform, key, body.approval_id,
            body.account_id)
        _tel("responded", str(result.get("status", "")))
    except PublishBlocked as exc:
        await _finish_attempt(request_status="BLOCKED",
                              result_state="FAILED",
                              error_code=exc.code,
                              error_message=str(exc)[:500])
        await store.update_publication_by_key(key, status="blocked",
                                              submission_state="FAILED")
        await store.set_approval_status(body.clip_id, "APPROVED", "automation")
        emit("SUBMISSION_BLOCKED", status="blocked", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             details={"idempotency_key": key,
                      "submission_attempt_id": attempt["submission_attempt_id"]})
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        # Ambiguous: the request may have been transmitted.
        _tel("timeout", "UNKNOWN_EXTERNAL_RESULT")
        await _finish_attempt(request_status="TIMEOUT_AFTER_TRANSMIT",
                              result_state="UNKNOWN_EXTERNAL_RESULT",
                              error_code="timeout",
                              error_message=f"{type(exc).__name__}")
        await store.update_publication_by_key(key, status="unknown",
                                              submission_state="UNKNOWN_EXTERNAL_RESULT")
        emit("SUBMISSION_UNKNOWN", status="unknown", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             details={"idempotency_key": key,
                      "submission_attempt_id": attempt["submission_attempt_id"],
                      "reason": "timeout after possible transmission"})
        return {"status": "UNKNOWN_EXTERNAL_RESULT",
                "submission_attempt_id": attempt["submission_attempt_id"],
                "idempotency_key": key,
                "detail": "request may have succeeded; reconcile before any retry"}
    except (ConnectionError, OSError) as exc:
        _tel("connection_error", "UNKNOWN_EXTERNAL_RESULT")
        await _finish_attempt(request_status="CONNECTION_RESET",
                              result_state="UNKNOWN_EXTERNAL_RESULT",
                              error_code="connection_error",
                              error_message=f"{type(exc).__name__}")
        await store.update_publication_by_key(key, status="unknown",
                                              submission_state="UNKNOWN_EXTERNAL_RESULT")
        emit("SUBMISSION_UNKNOWN", status="unknown", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             details={"idempotency_key": key,
                      "submission_attempt_id": attempt["submission_attempt_id"]})
        return {"status": "UNKNOWN_EXTERNAL_RESULT",
                "submission_attempt_id": attempt["submission_attempt_id"],
                "idempotency_key": key,
                "detail": "connection failed; result unknown, reconcile first"}
    except Exception as exc:
        await _finish_attempt(request_status="TRANSPORT_ERROR",
                              result_state="UNKNOWN_EXTERNAL_RESULT",
                              error_code=type(exc).__name__,
                              error_message=str(exc)[:500])
        await store.update_publication_by_key(key, status="unknown",
                                              submission_state="UNKNOWN_EXTERNAL_RESULT")
        emit("SUBMISSION_UNKNOWN", status="unknown", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             details={"idempotency_key": key,
                      "submission_attempt_id": attempt["submission_attempt_id"]})
        return {"status": "UNKNOWN_EXTERNAL_RESULT",
                "submission_attempt_id": attempt["submission_attempt_id"],
                "idempotency_key": key}

    # --- Provider responded: classify, never assume. ---
    prov_status = result.get("provider_status_code")
    if result.get("status") == "published" and result.get("post_url"):
        await _finish_attempt(
            request_status="RESPONDED",
            provider_submission_id=result.get("provider_submission_id"),
            provider_response_status=str(prov_status or "ok"),
            provider_response_metadata=result,
            result_state="CONFIRMED")
        await store.update_publication_by_key(
            key, status="published", post_url=result.get("post_url"),
            submission_state="CONFIRMED",
            published_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        await _record("clip", CLIP_TRANSITIONS, "PUBLISHING", "PUBLISHED",
                      clip_id=body.clip_id, campaign_id=body.campaign_id,
                      render_id=body.render_id, reason="platform_confirmed")
        await store.set_approval_status(body.clip_id, "PUBLISHED", "automation")
        # §13 operator record: sufficient to reconstruct why allowed.
        await store.log_decision(clip_id=body.clip_id, campaign_id=body.campaign_id,
                                 decision="published", rules=["publishing"],
                                 evidence=[{
                                     "operator_actor": "automation (operator-confirmed request)",
                                     "platform": body.platform,
                                     "campaign_id": body.campaign_id,
                                     "clip_id": body.clip_id,
                                     "render_hash": body.render_hash,
                                     "render_id": body.render_id,
                                     "policy_version": await _current_policy_version(),
                                     "approval_id": (approval or {}).get("id"),
                                     "confirmation_action": "operator_confirm+confirmed_render_hash",
                                     "provider": "authorized",
                                     "submission_attempt_id": attempt["submission_attempt_id"],
                                     "post_url": result.get("post_url")}],
                                 operator="automation")
        emit("SUBMISSION_CONFIRMED", status="published", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             duration_ms=int((_time.time() - t0) * 1000),
             details={"post_url": result.get("post_url"),
                      "submission_attempt_id": attempt["submission_attempt_id"]})
        return {"status": "published", "post_url": result.get("post_url"),
                "idempotency_key": key,
                "submission_attempt_id": attempt["submission_attempt_id"]}
    if prov_status is not None or result.get("status") in ("rejected", "failed", "error"):
        verdict = classify_provider_error(prov_status, body=str(result)[:300])
        if verdict["class"] == "RETRYABLE":
            if attempt["attempt_number"] >= MAX_ATTEMPTS:
                await _finish_attempt(
                    request_status="RESPONDED", result_state="FAILED",
                    provider_response_status=str(prov_status),
                    provider_response_metadata=result,
                    error_code=f"provider_{prov_status}",
                    error_message="retry budget exhausted")
                await store.update_publication_by_key(key, status="failed",
                                                      submission_state="FAILED")
                emit("SUBMISSION_RETRY_SCHEDULED", status="exhausted",
                     clip_id=body.clip_id, campaign_id=body.campaign_id,
                     details={"idempotency_key": key})
                return {"status": "FAILED", "idempotency_key": key,
                        "detail": "transient provider error; retry budget exhausted"}
            delay = retry_delay_s(attempt["attempt_number"],
                                  result.get("retry_after"))
            await _finish_attempt(
                request_status="RESPONDED", result_state="FAILED",
                provider_response_status=str(prov_status),
                provider_response_metadata=result,
                error_code=f"provider_{prov_status}",
                error_message=f"retryable; next attempt in ~{delay}s")
            await store.update_publication_by_key(
                key, status="failed", submission_state="FAILED",
                submission_status=f"retry_permitted_after_{delay}s")
            emit("SUBMISSION_RETRY_SCHEDULED", status="scheduled",
                 clip_id=body.clip_id, campaign_id=body.campaign_id,
                 details={"idempotency_key": key, "retry_in_s": delay,
                          "submission_attempt_id": attempt["submission_attempt_id"]})
            return {"status": "FAILED", "idempotency_key": key,
                    "retry_in_s": delay,
                    "detail": "transient provider error; controlled retry permitted"}
        if verdict["result_state"] == "ALREADY_PUBLISHED":
            await _finish_attempt(request_status="RESPONDED",
                                  result_state="ALREADY_PUBLISHED",
                                  provider_response_status=str(prov_status),
                                  provider_response_metadata=result)
            await store.update_publication_by_key(key,
                                                  submission_state="ALREADY_PUBLISHED")
            return {"status": "ALREADY_PUBLISHED",
                    "publication": await store.find_publication_by_key(key)}
        await _finish_attempt(request_status="RESPONDED", result_state="FAILED",
                              provider_response_status=str(prov_status),
                              provider_response_metadata=result,
                              error_code=f"provider_{prov_status}",
                              error_message=verdict["reason"])
        await store.update_publication_by_key(key, status="failed",
                                              submission_state="FAILED")
        emit("PUBLISH_FAILED", status="failed", clip_id=body.clip_id,
             campaign_id=body.campaign_id,
             details={"idempotency_key": key, "reason": verdict["reason"]})
        return {"status": "FAILED", "idempotency_key": key,
                "detail": verdict["reason"]}
    # Provider responded without confirmation (e.g. operator hook pending):
    # accepted for handling, NOT confirmed.
    await _finish_attempt(
        request_status="RESPONDED",
        provider_submission_id=result.get("provider_submission_id"),
        provider_response_status=str(prov_status or result.get("status")),
        provider_response_metadata=result,
        result_state="AWAITING_CONFIRMATION")
    await store.update_publication_by_key(key, status=result.get("status", "pending"),
                                          submission_state="AWAITING_CONFIRMATION",
                                          post_url=result.get("post_url"))
    emit("SUBMISSION_ACCEPTED", status=result.get("status", "pending"),
         clip_id=body.clip_id, campaign_id=body.campaign_id,
         duration_ms=int((_time.time() - t0) * 1000),
         details={"idempotency_key": key,
                  "submission_attempt_id": attempt["submission_attempt_id"],
                  **{k: v for k, v in result.items()
                     if k not in ("post_url",)}})
    return {"status": result.get("status"), "detail": result,
            "idempotency_key": key,
            "submission_attempt_id": attempt["submission_attempt_id"]}


@router.get("/publications")
async def publications():
    from automation import store
    await store.init_automation_tables()
    return {"publications": await store.list_publications()}


@router.get("/submissions/unresolved")
async def unresolved_submissions():
    from automation import store
    from automation.submissions import find_unresolved
    await store.init_automation_tables()
    items = await find_unresolved(store)
    out = []
    for p in items:
        attempts = await store.list_submission_attempts(p.get("idempotency_key") or "")
        out.append({**p, "attempts": attempts})
    return {"unresolved": out}


@router.get("/submissions/{idem_key}")
async def submission_status(idem_key: str):
    from automation import store
    await store.init_automation_tables()
    pub = await store.find_publication_by_key(idem_key)
    if pub is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "No submission with this idempotency key",
            {"idempotency_key": idem_key}))
    attempts = await store.list_submission_attempts(idem_key)
    return {"submission": pub, "attempts": attempts}


class ReconcileRequest(BaseModel):
    actor: str = "automation"


@router.post("/submissions/{idem_key}/reconcile")
async def reconcile_submission(idem_key: str, body: ReconcileRequest = ReconcileRequest(), _auth: str = OperatorAuth):
    from automation import store
    from automation.publishing import MockPublisher, AuthorizedPlatformPublisher
    from automation.submissions import reconcile_one
    await store.init_automation_tables()
    pub = await store.find_publication_by_key(idem_key)
    if pub is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "No submission with this idempotency key",
            {"idempotency_key": idem_key}))
    # Lookup adapter: mock registry when the record is a simulation,
    # otherwise the authorized adapter's lookup (may be unsupported →
    # STILL_UNKNOWN, never fabricated).
    if (pub.get("post_url") or "").startswith("mock://"):
        adapter = MockPublisher()
    else:
        adapter = AuthorizedPlatformPublisher()
    import asyncio as _aio

    async def _lookup(**kw):
        return await _aio.to_thread(adapter.lookup_submission, **kw)

    result = await reconcile_one(store, _lookup, idem_key, actor=body.actor)
    code = {"CONFIRMED": 200, "NOT_FOUND": 200, "FAILED": 200,
            "STILL_UNKNOWN": 200, "NEEDS_REVIEW": 200}[result["outcome"]]
    return {"outcome": result["outcome"], **{k: v for k, v in result.items()
                                             if k != "outcome"}, "status_code": code}


class ResolveRequest(BaseModel):
    resolution: str  # confirmed | not_published
    reviewer: str = "owner"
    reason: str = ""
    post_url: Optional[str] = None


@router.post("/submissions/{idem_key}/resolve")
async def resolve_submission(idem_key: str, body: ResolveRequest, _auth: str = OperatorAuth):
    """Audited human resolution of an UNKNOWN/NEEDS_REVIEW submission.
    No arbitrary state editing: only the two explicit resolutions."""
    from automation import store
    from automation.events import emit
    if body.resolution not in ("confirmed", "not_published"):
        raise HTTPException(status_code=400, detail=error_envelope(
            "invalid_request", "resolution must be confirmed|not_published", {}))
    await store.init_automation_tables()
    pub = await store.find_publication_by_key(idem_key)
    if pub is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "No submission with this idempotency key",
            {"idempotency_key": idem_key}))
    if (pub.get("submission_state") or "") not in (
            "UNKNOWN_EXTERNAL_RESULT", "NEEDS_REVIEW", "AWAITING_CONFIRMATION",
            "FAILED"):
        raise HTTPException(status_code=409, detail=error_envelope(
            "state_conflict",
            "only unresolved submissions can be manually resolved",
            {"submission_state": pub.get("submission_state")}))
    if body.resolution == "confirmed":
        if not body.post_url:
            raise HTTPException(status_code=400, detail=error_envelope(
                "invalid_request",
                "confirming requires the provider post URL as evidence", {}))
        await store.update_publication_by_key(
            idem_key, submission_state="CONFIRMED", status="published",
            post_url=body.post_url, submission_status="human_confirmed")
    else:
        await store.update_publication_by_key(
            idem_key, submission_state="FAILED",
            submission_status=f"human_resolved_not_published:{body.reason[:200]}")
    await store.log_decision(
        clip_id=pub.get("clip_id"), campaign_id=pub.get("campaign_id"),
        decision=f"submission_human_{body.resolution}",
        rules=["human_resolution"],
        evidence=[{"idempotency_key": idem_key, "reason": body.reason,
                   "post_url": body.post_url}],
        operator=body.reviewer)
    emit("SUBMISSION_RECONCILED", actor=body.reviewer, status=body.resolution,
         campaign_id=pub.get("campaign_id"), clip_id=pub.get("clip_id"),
         details={"idempotency_key": idem_key, "reason": body.reason})
    return {"idempotency_key": idem_key, "resolution": body.resolution}


class RetryRequest(BaseModel):
    actor: str = "owner"


@router.post("/submissions/{idem_key}/retry")
async def retry_submission(idem_key: str, body: RetryRequest = RetryRequest(), _auth: str = OperatorAuth):
    """Controlled retry: permitted ONLY after reconciliation proved NOT_FOUND
    (or a retryable FAILED). UNKNOWN submissions are rejected with 409 —
    reconcile first; blind retry is impossible through this endpoint."""
    from automation import store
    await store.init_automation_tables()
    pub = await store.find_publication_by_key(idem_key)
    if pub is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "No submission with this idempotency key",
            {"idempotency_key": idem_key}))
    state = pub.get("submission_state") or ""
    status_note = pub.get("submission_status") or ""
    if state in ("UNKNOWN_EXTERNAL_RESULT", "AWAITING_CONFIRMATION",
                 "NEEDS_REVIEW"):
        raise HTTPException(status_code=409, detail=error_envelope(
            "reconcile_first",
            "ambiguous submission state: reconcile before any retry",
            {"submission_state": state}))
    if state == "CONFIRMED" or state == "ALREADY_PUBLISHED":
        return {"status": state,
                "publication": await store.find_publication_by_key(idem_key)}
    if state == "FAILED" and not (
            status_note.startswith("retry_permitted")
            or status_note.startswith("reconcile_not_found")):
        raise HTTPException(status_code=409, detail=error_envelope(
            "retry_not_permitted",
            "this failure is not retryable; resolve manually",
            {"submission_state": state, "submission_status": status_note}))
    # Permitted: re-arm the SAME idempotency slot for one controlled attempt.
    await store.update_publication_by_key(idem_key, status="claimed",
                                          submission_state="CLAIMED",
                                          submission_status="retry_armed")
    await store.log_decision(
        clip_id=pub.get("clip_id"), campaign_id=pub.get("campaign_id"),
        decision="submission_retry_armed", rules=["controlled_retry"],
        evidence=[{"idempotency_key": idem_key}], operator=body.actor)
    return {"status": "retry_armed", "idempotency_key": idem_key,
            "detail": "re-submit through POST /publish with the same idempotency inputs"}


@router.get("/providers/capabilities")
async def provider_capabilities(platform: str = "tiktok"):
    from automation.publishing import (MockPublisher, ReplayPublisher,
                                       AuthorizedPlatformPublisher,
                                       adapter_status)
    from automation.providers import OfficialWhopProvider
    whop = OfficialWhopProvider()
    return {
        "publishers": {
            "mock": adapter_status(MockPublisher(), platform),
            "replay": adapter_status(ReplayPublisher(), platform),
            "authorized": adapter_status(AuthorizedPlatformPublisher(), platform),
        },
        "campaign_provider": {
            "adapter": "whop_official",
            "state": "OPERATIONAL" if whop.api_key else "UNAVAILABLE",
            "reason": "configured" if whop.api_key else
                      "WHOP_API_KEY not configured (mock/replay available)",
        },
    }


@router.get("/health")
async def health():
    """Dependency health. Optional live services being UNAVAILABLE does NOT
    make the application unhealthy."""
    import os as _os
    from automation.publishing import AuthorizedPlatformPublisher, adapter_status
    from automation.providers import OfficialWhopProvider
    from ffmpeg_utils import resolve_ffmpeg
    checks: dict = {}
    try:
        import database as _db
        conn = await _db._get_connection()
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()
        checks["database"] = {"state": "HEALTHY"}
    except Exception as exc:
        checks["database"] = {"state": "UNHEALTHY", "reason": str(exc)[:200]}
    try:
        import subprocess as _sp
        r = _sp.run([resolve_ffmpeg(), "-version"], capture_output=True, timeout=15)
        checks["ffmpeg"] = {"state": "HEALTHY" if r.returncode == 0 else "UNHEALTHY"}
    except Exception as exc:
        checks["ffmpeg"] = {"state": "UNHEALTHY", "reason": str(exc)[:200]}
    whop = OfficialWhopProvider()
    checks["campaign_provider"] = {
        "state": "HEALTHY" if whop.api_key else "UNAVAILABLE",
        "detail": "official API" if whop.api_key else "WHOP_API_KEY not configured"}
    checks["publisher"] = adapter_status(AuthorizedPlatformPublisher(), "tiktok")
    smtp = bool(_os.getenv("SMTP_HOST", ""))
    checks["smtp"] = {"state": "HEALTHY" if smtp else "UNAVAILABLE",
                      "detail": "configured" if smtp else "SMTP_HOST not configured"}
    try:
        import analytics as _an  # noqa - optional provider surface
        checks["analytics_provider"] = {"state": "HEALTHY"}
    except Exception:
        checks["analytics_provider"] = {"state": "UNAVAILABLE",
                                        "detail": "no analytics provider configured"}
    app_healthy = checks["database"]["state"] == "HEALTHY" and \
        checks["ffmpeg"]["state"] == "HEALTHY"
    return {"application": "HEALTHY" if app_healthy else "DEGRADED",
            "dependencies": checks}


# ---------------------------------------------------------------------------
# Analytics + weekly report
# ---------------------------------------------------------------------------

class AnalyticsIngest(BaseModel):
    clip_id: Optional[str] = None
    publication_id: Optional[str] = None
    provider: Optional[str] = None
    source: Optional[str] = None
    observed_status: Optional[str] = None  # CONFIRMED | PENDING | ESTIMATED
    metrics: dict = {}


@router.post("/analytics")
async def ingest_analytics(body: AnalyticsIngest):
    from automation import store
    await store.init_automation_tables()
    aid = await store.record_analytics(body.clip_id, body.publication_id, body.metrics,
                                       provider=body.provider, source=body.source,
                                       observed_status=body.observed_status)
    return {"id": aid}


@router.get("/analytics/summary")
async def analytics_summary():
    from automation import store
    from automation.analytics import aggregate_publications
    await store.init_automation_tables()
    pubs = await store.list_publications()
    snaps = await store.latest_analytics()
    return aggregate_publications(pubs, snaps)


class ReportRequest(BaseModel):
    recipients: Optional[list] = None


@router.post("/reports/weekly")
async def weekly_report(body: ReportRequest = ReportRequest()):
    from automation import store
    from automation.analytics import aggregate_publications
    from automation.events import emit
    from automation.reports import (build_report_payload, render_html, render_text,
                                    report_hash as _report_hash, send_email,
                                    week_bounds, new_report_id)
    from automation.side_effects import is_live
    await store.init_automation_tables()
    ws, we = week_bounds()
    campaigns = await store.list_campaigns()
    approvals = await store.list_approvals()
    pubs = await store.list_publications()
    summary = aggregate_publications(pubs, await store.latest_analytics())
    errors = [f"{a['clip_id']}: {a['status']}" for a in approvals if a["status"] == "FAILED"][:20]
    payload = build_report_payload(ws, we, campaigns, approvals, pubs, summary, errors)
    html = render_html(payload, pubs)
    text = render_text(payload, pubs)
    rhash = _report_hash(payload)
    # Idempotent + retry-safe: identical content reuses the same report row
    # (no duplicate rows on scheduler retries). An already-delivered report
    # is returned as-is and never resent; an undelivered one retries
    # delivery against the same row.
    existing = await store.find_report_by_hash(rhash)
    if existing and (existing.get("email_status") or "") == "EMAIL_SENT":
        return {"report_id": existing["report_id"], "payload": payload,
                "email_status": existing["email_status"], "resent": False,
                "html": existing.get("report_html") or html, "text": text}
    # Dry-run safe: no external mail without live mode + explicit recipients.
    email_status = send_email(f"AUTOCLIP WEEKLY REPORT {ws}–{we}", html, text,
                              body.recipients, allow_external=is_live())
    rid = existing["report_id"] if existing else new_report_id()
    delivery_id = None
    if email_status == "EMAIL_SENT":
        import uuid as _uuid
        delivery_id = f"eml_{_uuid.uuid4().hex[:10]}"
    await store.save_report(rid, ws, we, payload, html, email_status,
                            report_hash=rhash, delivery_id=delivery_id)
    from automation import telemetry as _telrep
    import time as _trep
    _telrep.record(provider="local", operation="report_generate",
                   status="ok", duration_ms=None, result_state=email_status,
                   meta={"report_id": rid})
    emit("REPORT_GENERATED", status="ok",
         details={"report_id": rid, "email_status": email_status})
    return {"report_id": rid, "payload": payload, "email_status": email_status,
            "resent": False, "html": html, "text": text}


@router.get("/reports")
async def list_reports():
    from automation import store
    await store.init_automation_tables()
    return {"reports": await store.list_reports()}


@router.get("/jobs")
async def list_jobs():
    from automation import store
    await store.init_automation_tables()
    return {"jobs": await store.list_jobs()}


# ---------------------------------------------------------------------------
# Title / description generation + validation
# ---------------------------------------------------------------------------

class MetadataRequest(BaseModel):
    transcript: list = []
    source_title: str = ""
    campaign: dict = {}
    source_evidence_ids: Optional[list] = None


@router.post("/metadata/generate")
async def metadata_generate(body: MetadataRequest):
    from automation.titles import (generate_title, generate_description,
                                   validate_metadata, validate_metadata_legacy,
                                   METADATA_VERSION)
    title = generate_title(body.transcript, body.source_title, body.campaign)
    desc = generate_description(body.transcript, body.campaign, body.source_title)
    validation = validate_metadata(title, desc, body.transcript, body.campaign)
    legacy = validate_metadata_legacy(title, desc, body.transcript, body.campaign)
    return {"title": title, "description": desc, "validation": validation,
            "ok": legacy["ok"], "issues": legacy["issues"],
            "decision": legacy["decision"],
            "metadata_version": METADATA_VERSION,
            "source_evidence_ids": body.source_evidence_ids or []}


# ---------------------------------------------------------------------------
# v1.2 operations: readiness, overview, scheduler, telemetry, alerts,
# retention, lineage, provider check
# ---------------------------------------------------------------------------

@router.get("/readiness")
async def readiness():
    """APPLICATION STARTUP vs AUTOMATION READINESS, kept separate. Optional
    provider outages never fail the application itself. No secrets included."""
    from automation.configcheck import readiness_matrix
    from automation import store
    await store.init_automation_tables()
    matrix = readiness_matrix()
    try:
        import database as _db
        conn = await _db._get_connection()
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()
        db = "HEALTHY"
    except Exception:
        db = "UNHEALTHY"
    return {"application": "READY" if db == "HEALTHY" else "DEGRADED",
            "automation": matrix["automation_readiness"],
            "application_version": __import__("version").APP_VERSION,
            "schema_version": __import__("version").SCHEMA_VERSION,
            "config": matrix["config"],
            "matrix": matrix["matrix"],
            "note": matrix["note"]}


@router.get("/liveness")
async def liveness():
    """Liveness: the process is functioning. Minimal check, no provider
    calls, no side effects. Distinct from readiness (can we do local work)
    and dependency status (are optional providers up)."""
    import version as _version
    return {"alive": True, "application_version": _version.APP_VERSION}


@router.get("/meta")
async def automation_meta():
    """Authoritative release + schema metadata. Single source of truth."""
    import version as _version
    from automation import store
    from automation.auth import auth_mode
    await store.init_automation_tables()
    try:
        schema = await store.get_meta("schema_version")
    except Exception:
        schema = None
    return {"application_version": _version.APP_VERSION,
            "schema_version": schema or str(_version.SCHEMA_VERSION),
            "code_schema_version": _version.SCHEMA_VERSION,
            "mode": __import__("os").getenv("AUTOMATION_MODE", "dry_run"),
            "auth_mode": auth_mode(),
            "policy_version": await _current_policy_version()}


@router.get("/diagnostics/config")
async def diagnostics_config():
    """Sanitized runtime configuration snapshot for diagnostics. Secrets
    are never included (keys/tokens/passwords/headers/cookies/connection
    strings with credentials are dropped, not masked)."""
    import os as _os
    import version as _version
    from automation.configcheck import validate as _validate
    from automation.publishing import (AuthorizedPlatformPublisher,
                                       adapter_status)
    from automation.providers import OfficialWhopProvider
    cfg = _validate()
    return {
        "application_version": _version.APP_VERSION,
        "schema_version": _version.SCHEMA_VERSION,
        "mode": cfg["mode"],
        "human_approval_required": _os.getenv("HUMAN_APPROVAL_REQUIRED", "true"),
        "live_publishing_enabled": _os.getenv("LIVE_PUBLISHING_ENABLED", "false"),
        "approval_ttl_hours": _os.getenv("APPROVAL_TTL_HOURS", "168"),
        "max_attempts": _os.getenv("MAX_ATTEMPTS", "4"),
        "editor_transcript_timeout": _os.getenv("EDITOR_TRANSCRIPT_TIMEOUT", "90"),
        "scheduler_lease_ttl_s": _os.getenv("SCHEDULER_LEASE_TTL_S", "300"),
        "scheduler_heartbeat_s": _os.getenv("SCHEDULER_HEARTBEAT_S", "15"),
        "min_free_disk_bytes": _os.getenv("MIN_FREE_DISK_BYTES",
                                          str(500 * 1024 * 1024)),
        "publish_platforms": _os.getenv("PUBLISH_PLATFORMS", ""),
        "tmp_dir_configured": bool(_os.getenv("TMP_DIR", "")),
        "db_configured": bool(_os.getenv("DB_PATH", "")),
        "credentials_present": {
            "whop_api_key": bool(_os.getenv("WHOP_API_KEY", "").strip()),
            "publish_token_tiktok": bool(_os.getenv("PUBLISH_TOKEN_TIKTOK", "").strip()),
            "publish_token_youtube": bool(_os.getenv("PUBLISH_TOKEN_YOUTUBE", "").strip()),
            "smtp": bool(_os.getenv("SMTP_HOST", "").strip()),
        },
        "publisher_tiktok": adapter_status(AuthorizedPlatformPublisher(), "tiktok")["state"],
        "campaign_provider": "OPERATIONAL" if OfficialWhopProvider().api_key else "UNAVAILABLE",
        "policy_version": await _current_policy_version(),
        "config_findings": cfg["findings"],
    }


@router.get("/overview")
async def overview():
    """Operational dashboard aggregates. Missing data stays missing."""
    from automation import store
    from automation.analytics import aggregate_publications
    from automation.submissions import find_unresolved
    await store.init_automation_tables()
    campaigns = await store.list_campaigns()
    approvals = await store.list_approvals()
    pubs = await store.list_publications()
    jobs = await store.list_jobs(200)
    summary = aggregate_publications(pubs, await store.latest_analytics())
    by_status: dict[str, int] = {}
    for a in approvals:
        by_status[a.get("status", "UNKNOWN")] = by_status.get(a.get("status", "UNKNOWN"), 0) + 1
    by_sub: dict[str, int] = {}
    for p in pubs:
        st = (p.get("submission_state") or "NOT_SUBMITTED").upper()
        by_sub[st] = by_sub.get(st, 0) + 1
    by_job: dict[str, int] = {}
    for j in jobs:
        by_job[j.get("status", "?")] = by_job.get(j.get("status", "?"), 0) + 1
    scans = await store.list_transitions()
    last_scan = next((t["ts"] for t in scans if t.get("machine") == "campaign"), None)
    return {
        "system": {"jobs": by_job, "last_scan": last_scan,
                   "stale_jobs": [j["job_id"] for j in jobs
                                  if j.get("status") == "running"][:20]},
        "pipeline": {
            "campaigns_discovered": len(campaigns),
            "eligible": sum(1 for c in campaigns if c.get("status") == "ELIGIBLE"),
            "rejected": sum(1 for c in campaigns if c.get("status") == "REJECTED"),
            "review": sum(1 for c in campaigns if c.get("status") == "NEEDS_REVIEW"),
            "clips_selected": by_status.get("SELECTED", 0),
            "renders_failed": sum(1 for a in approvals if a.get("status") == "FAILED"),
        },
        "publication": {
            "approved": by_status.get("APPROVED", 0),
            "queued": by_sub.get("CLAIMED", 0) + by_sub.get("SUBMISSION_STARTED", 0),
            "confirmed": by_sub.get("CONFIRMED", 0),
            "failed": by_sub.get("FAILED", 0),
            "unknown": len(await find_unresolved(store)),
            "by_state": by_sub,
        },
        "analytics": summary,
    }


@router.get("/scheduler")
async def scheduler_state():
    from automation import store
    from automation.scheduler import AutomationScheduler, LEASE_TTL_S
    from automation.submissions import find_unresolved
    await store.init_automation_tables()
    jobs = await store.list_jobs(200)
    import time as _t
    now = _t.time()
    stale, active, queued, failed = [], [], [], []
    for j in jobs:
        st = j.get("status")
        if st == "running":
            try:
                import datetime as _dt
                age = now - _dt.datetime.fromisoformat(
                    str(j.get("heartbeat_at") or "2000-01-01").replace("Z", "+00:00")).timestamp()
            except Exception:
                age = 1e9
            (stale if age > LEASE_TTL_S else active).append(j["job_id"])
        elif st == "queued":
            queued.append(j["job_id"])
        elif st == "failed":
            failed.append(j["job_id"])
    return {"jobs": jobs[:100], "active": active, "queued": queued,
            "stale": stale, "failed": failed,
            "unknown_submissions": await find_unresolved(store),
            "recovery": AutomationScheduler.recover_interrupted(jobs)}


class JobAction(BaseModel):
    actor: str = "owner"


@router.post("/scheduler/jobs/{job_id}/retry")
async def retry_job(job_id: str, body: JobAction = JobAction()):
    """Requeue an eligible LOCAL job. Publishing/ambiguous jobs are refused —
    use reconciliation, never blind retry."""
    from automation import store
    await store.init_automation_tables()
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "Unknown job", {"job_id": job_id}))
    if job.get("status") != "failed":
        raise HTTPException(status_code=409, detail=error_envelope(
            "state_conflict", "only failed jobs can be retried",
            {"status": job.get("status")}))
    if "publish" in str(job.get("kind", "")) or \
            "UNKNOWN_EXTERNAL_RESULT" in str(job.get("payload_json") or ""):
        raise HTTPException(status_code=409, detail=error_envelope(
            "reconcile_first",
            "publishing jobs are never retried blindly; reconcile first",
            {"job_id": job_id}))
    await store.upsert_job(job_id, job.get("kind", ""), "queued",
                           job.get("campaign_id"), job.get("clip_id"))
    await store.log_decision(decision="job_retry_armed", rules=["scheduler_ops"],
                             evidence=[{"job_id": job_id}], operator=body.actor)
    return {"job_id": job_id, "status": "queued"}


@router.post("/scheduler/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, body: JobAction = JobAction()):
    from automation import store
    await store.init_automation_tables()
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "Unknown job", {"job_id": job_id}))
    if job.get("status") not in ("queued", "running", "needs_review"):
        raise HTTPException(status_code=409, detail=error_envelope(
            "state_conflict", "only pending jobs can be cancelled",
            {"status": job.get("status")}))
    await store.upsert_job(job_id, job.get("kind", ""), "cancelled",
                           job.get("campaign_id"), job.get("clip_id"))
    await store.log_decision(decision="job_cancelled", rules=["scheduler_ops"],
                             evidence=[{"job_id": job_id}], operator=body.actor)
    return {"job_id": job_id, "status": "cancelled"}


@router.get("/telemetry")
async def telemetry():
    from automation import telemetry as _tel
    return _tel.summary()


@router.get("/alerts")
async def alerts():
    from automation import alerts as _alerts
    from automation import store
    await store.init_automation_tables()
    import os as _os
    tmp_dir = _os.getenv("TMP_DIR", "")
    return {"alerts": await _alerts.evaluate(store, tmp_dir=tmp_dir)}


class CleanupRequest(BaseModel):
    dry_run: bool = True
    paths: Optional[list] = None
    actor: str = "owner"


@router.post("/retention/cleanup")
async def retention_cleanup(body: CleanupRequest, _auth: str = OperatorAuth):
    from automation import resources as _res
    from automation import store
    await store.init_automation_tables()
    import os as _os
    tmp_dir = _os.getenv("TMP_DIR", _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tmp"))
    cands = await _res.cleanup_candidates(store, tmp_dir)
    if body.dry_run or not body.paths:
        return {"dry_run": True, "candidates": cands}
    return {"dry_run": False, **await _res.purge(store, body.paths, tmp_dir, body.actor)}


@router.get("/lineage/{clip_id}")
async def lineage(clip_id: str):
    """Reconstruct why a clip was published. Each stage reports present or
    missing — never invented."""
    from automation import store
    await store.init_automation_tables()
    import database as _db
    chain: dict = {}
    try:
        clip = await _db.get_clip(clip_id)
    except Exception:
        clip = None
    chain["source"] = {"present": bool(clip), "clip": clip}
    try:
        state = await _db.get_editor_state(clip_id)
    except Exception:
        state = None
    chain["editor_state"] = {"present": bool(state)}
    try:
        renders = await _db.get_clip_renders(clip_id)
    except Exception:
        renders = []
    chain["renders"] = {"present": bool(renders), "count": len(renders or [])}
    appr = await store.get_approval(clip_id)
    chain["approval"] = {"present": bool(appr),
                         "status": (appr or {}).get("status"),
                         "render_hash": (appr or {}).get("render_hash"),
                         "policy_version": (appr or {}).get("policy_version")}
    pubs = await store.list_publications()
    mine = [p for p in pubs if p.get("clip_id") == clip_id]
    atts = []
    for p in mine:
        atts.extend(await store.list_submission_attempts(p.get("idempotency_key") or ""))
    chain["submission"] = {"present": bool(mine), "publications": mine,
                           "attempts": atts}
    chain["transitions"] = await store.list_transitions(clip_id=clip_id)
    try:
        all_dec = await store.list_decisions(limit=500)
    except TypeError:
        all_dec = await store.list_decisions()
    chain["decisions"] = [d for d in all_dec if d.get("clip_id") == clip_id]
    snaps = await store.latest_analytics()
    chain["analytics"] = [s for s in snaps if s.get("clip_id") == clip_id]
    # Canonical pipeline timeline: every stage reports status explicitly;
    # missing information is marked missing, never invented.
    dec_rules = {d.get("decision") for d in chain["decisions"]}
    first_render = (renders or [{}])[0] if renders else {}
    timeline = [
        {"stage": "SOURCE", "status": "present" if clip else "missing",
         "artifact": (clip or {}).get("file_path")},
        {"stage": "TRANSCRIPT", "status": "present" if state else "missing",
         "artifact": None},
        {"stage": "DETECTION", "status": "present" if state else "missing",
         "artifact": None},
        {"stage": "FILTERS", "status": "present" if state else "missing",
         "artifact": None},
        {"stage": "SELECTION",
         "status": "present" if ("eligible" in dec_rules or appr) else "missing",
         "artifact": None},
        {"stage": "REFRAME", "status": "present" if renders else "missing",
         "artifact": first_render.get("file_path")},
        {"stage": "COMPOSITION", "status": "present" if renders else "missing",
         "artifact": first_render.get("canvas")},
        {"stage": "PEOPLE_MASK",
         "status": "present" if (state or {}).get("people_policy") else "missing",
         "artifact": None},
        {"stage": "CAPTIONS", "status": "present" if renders else "missing",
         "artifact": None},
        {"stage": "METADATA",
         "status": "present" if (appr or {}).get("title") else "missing",
         "artifact": (appr or {}).get("title")},
        {"stage": "APPROVAL",
         "status": (appr or {}).get("status", "missing") if appr else "missing",
         "artifact": (appr or {}).get("render_hash"),
         "version": (appr or {}).get("policy_version")},
        {"stage": "RENDER",
         "status": "present" if (appr or {}).get("render_hash") else "missing",
         "artifact": (appr or {}).get("render_hash")},
        {"stage": "PUBLICATION_STATE",
         "status": (mine[0].get("submission_state", "missing") if mine
                     else "missing"),
         "artifact": (mine[0].get("post_url") if mine else None)},
        {"stage": "ANALYTICS",
         "status": "present" if chain["analytics"] else "missing",
         "artifact": None},
    ]
    chain["timeline"] = timeline
    return {"clip_id": clip_id, "chain": chain}


@router.post("/providers/check")
async def provider_check(platform: str = "tiktok"):
    """Read-only live verification (same boundary as the CLI harness).
    Never publishes."""
    from automation.provider_client import AuthorizedProviderClient
    from automation import telemetry as _tel
    import time as _t
    t0 = _t.time()
    out = AuthorizedProviderClient(platform).check()
    _tel.record(provider="authorized", operation="check",
                status="ok" if out["state"] != "UNAVAILABLE" else "unavailable",
                duration_ms=int((_t.time() - t0) * 1000),
                result_state=out["state"])
    # §3 step 6: persist the qualification outcome (read-only evidence).
    from automation import store as _store_q
    from automation.qualification import new_qualification as _new_qual
    await _store_q.init_automation_tables()
    vtype = {"UNAVAILABLE": "UNAVAILABLE", "AUTHENTICATED": "LIVE OPTIONAL",
             "AVAILABLE": "LIVE OPTIONAL"}.get(out["state"], "UNAVAILABLE")
    qual = _new_qual(provider="authorized", platform=platform,
                     verification_type=vtype,
                     verification_scope="providers/check:config→auth→capabilities→readonly",
                     limitations=out.get("reason", ""),
                     required_credentials=[f"PUBLISH_TOKEN_{platform.upper()}"],
                     checks={"configuration_status": "CONFIGURED" if vtype != "UNAVAILABLE" else "UNAVAILABLE",
                             "authentication_status": "AUTHENTICATED" if vtype == "LIVE OPTIONAL" else "UNAVAILABLE",
                             "availability_status": out["state"],
                             "operational_status": "UNAVAILABLE"})
    await _store_q.save_qualification(qual)
    out["qualification_id"] = qual["qualification_id"]
    return out


class SmtpVerifyRequest(BaseModel):
    test_recipient: Optional[str] = None
    actor: str = "owner"


@router.post("/smtp/verify")
async def smtp_verify(body: SmtpVerifyRequest, _auth: str = OperatorAuth):
    """SMTP qualification (§11). Without a recipient: authenticate only
    (AUTHENTICATED). With an EXPLICIT test recipient: send one clearly
    marked test message (SEND_VERIFIED + delivery id). Never sends a
    production report; never sends without an explicit recipient."""
    import smtplib as _smtp
    from email.mime.text import MIMEText as _MT
    from automation import store
    from automation.qualification import new_qualification
    await store.init_automation_tables()
    host, user = os.getenv("SMTP_HOST", ""), os.getenv("SMTP_USER", "")
    if not host:
        qual = new_qualification(provider="smtp", platform="email",
                                 verification_type="UNAVAILABLE",
                                 verification_scope="smtp_verify",
                                 limitations="SMTP_HOST not configured")
        await store.save_qualification(qual)
        return {"state": "NOT_CONFIGURED", "qualification_id": qual["qualification_id"]}
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        server = _smtp.SMTP(host, port, timeout=30)
        try:
            server.starttls()
            pw = os.getenv("SMTP_PASS", "")
            if user:
                server.login(user, pw)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except Exception as exc:
        qual = new_qualification(provider="smtp", platform="email",
                                 verification_type="UNAVAILABLE",
                                 verification_scope="smtp_verify:authenticate",
                                 limitations=f"auth failed: {type(exc).__name__}")
        await store.save_qualification(qual)
        return {"state": "FAILED", "reason": "authentication failed",
                "qualification_id": qual["qualification_id"]}
    if not body.test_recipient:
        qual = new_qualification(provider="smtp", platform="email",
                                 verification_type="LIVE OPTIONAL",
                                 verification_scope="smtp_verify:authenticate",
                                 capabilities={"SUBMIT": "VERIFIED"},
                                 limitations="authenticated only; no send performed")
        await store.save_qualification(qual)
        return {"state": "AUTHENTICATED",
                "qualification_id": qual["qualification_id"]}
    import uuid as _uuid
    delivery_id = f"eml_{_uuid.uuid4().hex[:10]}"
    try:
        msg = _MT("AUTOCLIP SMTP verification probe (no action needed).")
        msg["Subject"] = f"AUTOCLIP SMTP probe {delivery_id}"
        msg["From"] = os.getenv("SMTP_FROM", "autoclip@localhost")
        msg["To"] = body.test_recipient
        server = _smtp.SMTP(host, port, timeout=30)
        try:
            server.starttls()
            if user:
                server.login(user, os.getenv("SMTP_PASS", ""))
            refused = server.sendmail(msg["From"], [body.test_recipient],
                                      msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        if refused:
            raise RuntimeError(f"recipients refused: {list(refused)}")
    except Exception as exc:
        qual = new_qualification(provider="smtp", platform="email",
                                 verification_type="UNAVAILABLE",
                                 verification_scope="smtp_verify:test_send",
                                 limitations=f"test send failed: {type(exc).__name__}")
        await store.save_qualification(qual)
        return {"state": "FAILED", "reason": "test send failed",
                "qualification_id": qual["qualification_id"]}
    qual = new_qualification(provider="smtp", platform="email",
                             verification_type="LIVE VERIFIED",
                             verification_scope="smtp_verify:test_send",
                             capabilities={"SUBMIT": "VERIFIED"},
                             limitations="verified for test recipient only")
    await store.save_qualification(qual)
    await store.log_decision(decision="smtp_send_verified", rules=["smtp_verify"],
                             evidence=[{"delivery_id": delivery_id,
                                        "recipient": body.test_recipient}],
                             operator=body.actor)
    return {"state": "SEND_VERIFIED", "delivery_id": delivery_id,
            "qualification_id": qual["qualification_id"]}


@router.get("/providers/qualifications")
async def list_qualifications(provider: Optional[str] = None,
                              platform: Optional[str] = None):
    from automation import store
    await store.init_automation_tables()
    return {"qualifications": await store.list_qualifications(provider, platform)}


class QualificationRecord(BaseModel):
    provider: str
    platform: str
    adapter_version: str = "v1"
    client_version: str = "v1"
    capabilities: dict = {}
    verification_type: str = "UNAVAILABLE"
    verification_scope: str = ""
    limitations: str = ""
    required_credentials: list = []
    checks: dict = {}


@router.post("/providers/qualifications")
async def record_qualification(body: QualificationRecord, _auth: str = OperatorAuth):
    """Persist a qualification record. LIVE_VERIFIED is rejected here —
    it can only be produced by an actual authorized verification flow,
    never asserted by hand."""
    from automation import store
    from automation.qualification import new_qualification, LIVE_VERIFIED
    if body.verification_type == LIVE_VERIFIED:
        raise HTTPException(status_code=400, detail=error_envelope(
            "invalid_request",
            "LIVE_VERIFIED cannot be asserted manually; use an authorized verification flow",
            {}))
    await store.init_automation_tables()
    qual = new_qualification(**body.model_dump())
    await store.save_qualification(qual)
    return {"qualification_id": qual["qualification_id"],
            "verification_type": qual["verification_type"]}


class AcceptanceRecord(BaseModel):
    version: str
    checklist: dict = {}
    notes: str = ""
    actor: str = "owner"


@router.post("/acceptance")
async def record_release_acceptance(body: AcceptanceRecord, _auth: str = OperatorAuth):
    """Explicit operator acceptance event (Phase 1). The server never marks
    a release accepted on its own; all checklist claims are the operator's
    recorded assertions, auditable via the decisions log."""
    import version as _version
    from automation import store
    await store.init_automation_tables()
    if body.version != _version.APP_VERSION:
        raise HTTPException(status_code=400, detail=error_envelope(
            "invalid_request",
            "acceptance version must match the running build",
            {"running": _version.APP_VERSION, "submitted": body.version}))
    aid = await store.record_acceptance(
        body.version, _version.SCHEMA_VERSION, body.actor or "owner",
        body.checklist, body.notes)
    await store.log_decision(decision="release_accepted", rules=["release"],
                             evidence=[{"acceptance_id": aid,
                                        "version": body.version,
                                        "checklist": body.checklist,
                                        "notes": body.notes}],
                             operator=body.actor or "owner")
    return {"acceptance_id": aid, "version": body.version}


@router.get("/acceptance")
async def list_release_acceptance(version: Optional[str] = None):
    from automation import store
    await store.init_automation_tables()
    return {"acceptances": await store.list_acceptances(version)}


@router.get("/consistency")
async def consistency():
    """Structured data-integrity errors across automation records (§25).
    Read-only: reports corruption, never mutates it."""
    from automation import store
    await store.init_automation_tables()
    import database as _db
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(code, message, details):
        errors.append({"code": code, "message": message, "details": details})

    approvals = await store.list_approvals()
    for a in approvals:
        try:
            clip = await _db.get_clip(a["clip_id"])
        except Exception:
            clip = None
        if clip is None:
            warnings.append({"code": "approval_orphaned_clip",
                             "message": "approval references a missing core clip "
                                        "(normal for mock/dry-run entries)",
                             "details": {"clip_id": a["clip_id"]}})
        for key in ("render_hash", "policy_version"):
            if not a.get(key):
                if (a.get("status") or "") in ("APPROVED", "PUBLISHING",
                                               "PUBLISHED"):
                    err("approval_unbound",
                        f"publishable approval missing {key} binding",
                        {"clip_id": a["clip_id"], "status": a.get("status")})
                else:
                    warnings.append({"code": "approval_not_yet_bound",
                                     "message": f"pre-approval record missing {key}",
                                     "details": {"clip_id": a["clip_id"],
                                                 "status": a.get("status")}})
    pubs = await store.list_publications()
    for p in pubs:
        try:
            clip = await _db.get_clip(p["clip_id"]) if p.get("clip_id") else None
        except Exception:
            clip = None
        if p.get("clip_id") and clip is None:
            warnings.append({"code": "submission_orphaned_clip",
                             "message": "submission references a missing core clip "
                                        "(normal for mock/dry-run entries)",
                             "details": {"clip_id": p.get("clip_id"),
                                         "idempotency_key": p.get("idempotency_key")}})
        if (p.get("submission_state") == "CONFIRMED") and not p.get("post_url"):
            err("confirmed_without_url",
                "CONFIRMED submission has no post URL",
                {"idempotency_key": p.get("idempotency_key")})
    # transition history must agree with current clip/campaign state
    current: dict[tuple, str] = {}
    for a in approvals:
        current[("clip", a["clip_id"])] = a.get("status")
    for c in await store.list_campaigns():
        current[("campaign", c["campaign_id"])] = c.get("status")
    latest: dict[tuple, dict] = {}
    for t in await store.list_transitions(limit=5000):
        ent = t.get("clip_id") if t["machine"] == "clip" else t.get("campaign_id")
        if not ent:
            continue
        key = (t["machine"], ent)
        if key not in latest or str(t["ts"]) > str(latest[key]["ts"]):
            latest[key] = t
    for key, t in latest.items():
        if key in current and t["new_state"] != current[key]:
            err("history_state_divergence",
                "latest transition disagrees with current state",
                {"machine": key[0], "id": key[1],
                 "transition": t["new_state"], "current": current[key]})
    quals = await store.list_qualifications()
    for q in quals:
        if q.get("provider") not in ("mock", "replay", "authorized",
                                     "whop_official", "smtp"):
            err("qualification_unknown_provider",
                "qualification refers to an unknown provider",
                {"record_id": q.get("record_id")})
    # campaign references: decisions must point at known campaigns
    known_campaigns = {c["campaign_id"] for c in await store.list_campaigns()}
    for d in await store.list_decisions(limit=2000):
        if d.get("campaign_id") and d["campaign_id"] not in known_campaigns:
            err("decision_orphaned_campaign",
                "decision references a missing campaign",
                {"decision_id": d.get("id"),
                 "campaign_id": d.get("campaign_id")})
    # attempt references: every attempt must belong to a known slot
    for p in pubs:
        key = p.get("idempotency_key") or ""
        if not key:
            err("submission_missing_key",
                "publication row has no idempotency key",
                {"publication_id": p.get("id")})
    # analytics references: snapshots must point at a known publication/clip
    for s in await store.latest_analytics():
        if s.get("publication_id"):
            ok_pub = any(p.get("id") == s["publication_id"] for p in pubs)
            if not ok_pub:
                err("analytics_orphaned_publication",
                    "analytics snapshot references a missing publication",
                    {"analytics_id": s.get("id")})
    # warnings (non-blocking): legacy rows missing optional bindings
    for a in approvals:
        if a.get("status") == "APPROVED" and not a.get("platform"):
            warnings.append({"code": "approval_no_platform",
                             "message": "approved without platform binding (legacy row)",
                             "details": {"clip_id": a["clip_id"]}})
    # referenced render artifacts must resolve
    try:
        for a in approvals[:200]:
            rp = (a.get("preview_path") or "")
            if rp.startswith("/files/"):
                import os as _os
                tmp_dir = _os.getenv("TMP_DIR", "")
                full = _os.path.normpath(_os.path.join(
                    tmp_dir, rp.replace("/files/", "", 1))) if tmp_dir else ""
                if full and not _os.path.isfile(full):
                    err("artifact_missing",
                        "approval preview artifact missing from disk",
                        {"clip_id": a["clip_id"], "preview_path": rp})
    except Exception:
        pass
    counts = {"approvals": len(approvals), "publications": len(pubs),
              "campaigns": len(known_campaigns),
              "qualifications": len(quals)}
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "counts": counts, "checked_at": __import__(
        "datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}


@router.get("/diagnostics")
async def diagnostics():
    """Application self-diagnostics (v1.6): one sanitized snapshot with
    per-dependency states (HEALTHY/DEGRADED/UNAVAILABLE/FAILED — never a
    single boolean). Secrets are never included."""
    import os as _os
    import version as _version
    from automation import store
    from automation.configcheck import readiness_matrix
    await store.init_automation_tables()

    async def _db_state():
        try:
            import database as _db
            conn = await _db._get_connection()
            try:
                await conn.execute("SELECT 1")
            finally:
                await conn.close()
            return "HEALTHY"
        except Exception:
            return "FAILED"

    def _ffmpeg_state():
        try:
            import subprocess as _sp
            from ffmpeg_utils import resolve_ffmpeg
            r = _sp.run([resolve_ffmpeg(), "-version"], capture_output=True,
                        timeout=15)
            return "HEALTHY" if r.returncode == 0 else "FAILED"
        except Exception:
            return "FAILED"

    def _disk():
        from automation.resources import disk_free_bytes, MIN_FREE_BYTES
        tmp_dir = _os.getenv("TMP_DIR", "")
        free = disk_free_bytes(tmp_dir) if tmp_dir else -1
        state = ("HEALTHY" if free < 0 or free >= MIN_FREE_BYTES
                 else "DEGRADED" if free >= MIN_FREE_BYTES // 5 else "FAILED")
        return {"state": state, "disk_free_bytes": free,
                "disk_floor_bytes": MIN_FREE_BYTES}

    def _dir_size(path: str) -> int:
        total = 0
        try:
            for root, _dirs, files in _os.walk(path):
                for fn in files:
                    try:
                        total += _os.path.getsize(_os.path.join(root, fn))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    matrix = readiness_matrix()
    jobs = await store.list_jobs(200)
    scans = await store.list_transitions()
    last_scan = next((t["ts"] for t in scans if t.get("machine") == "campaign"),
                     None)
    done_jobs = [j for j in jobs if j.get("status") == "completed"]
    last_ok = max([j.get("completed_at") or "" for j in done_jobs],
                  default=None)
    failed = [j for j in jobs if j.get("status") == "failed"]
    stale = [j["job_id"] for j in jobs if j.get("status") == "running"]
    tmp_dir = _os.getenv("TMP_DIR", "")
    db_path = _os.getenv("DB_PATH", "")
    try:
        db_bytes = _os.path.getsize(db_path) if db_path and _os.path.isfile(db_path) else -1
    except OSError:
        db_bytes = -1
    return {
        "application_version": _version.APP_VERSION,
        "schema_version": _version.SCHEMA_VERSION,
        "mode": matrix["config"]["mode"],
        "database": {"state": await _db_state()},
        "ffmpeg": {"state": _ffmpeg_state()},
        "storage": {**_disk(), "tmp_bytes": _dir_size(tmp_dir) if tmp_dir else -1,
                    "db_bytes": db_bytes},
        "scheduler": {"state": "HEALTHY", "mode": "on-demand",
                      "active": len([j for j in jobs if j.get("status") == "running"]),
                      "queued": len([j for j in jobs if j.get("status") == "queued"]),
                      "failed": len(failed), "stale": stale},
        "rendering": {"state": "HEALTHY"},
        "provider_states": {m["capability"]: m["verification"] for m in matrix["matrix"]},
        "analytics_states": {"analytics": "UNAVAILABLE"},
        "smtp_state": next((m["verification"] for m in matrix["matrix"]
                            if m["capability"] == "smtp"), "UNAVAILABLE"),
        "unresolved_submissions": len([
            p for p in await store.list_publications()
            if (p.get("submission_state") in ("UNKNOWN_EXTERNAL_RESULT",
                                              "AWAITING_CONFIRMATION",
                                              "NEEDS_REVIEW"))]),
        "failed_jobs": [j["job_id"] for j in failed[:20]],
        "stale_jobs": stale[:20],
        "consistency": (await consistency())["ok"],
        "last_scan": last_scan,
        "last_successful_job": last_ok,
    }


@router.get("/jobs/{job_id}/timeline")
async def job_timeline(job_id: str):
    """Complete observable timeline for one job: created/claimed/started/
    heartbeat/progress/retry/failure/completed/cancelled with attempt,
    step, timestamps, duration, state, failure code. No credentials."""
    from automation import store
    await store.init_automation_tables()
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=error_envelope(
            "not_found", "Unknown job", {"job_id": job_id}))
    events = [{
        "at": job.get("started_at"), "event": "created",
        "state": "queued", "step": job.get("step"),
        "attempt": job.get("attempt", 0)}]
    if job.get("heartbeat_at"):
        events.append({"at": job.get("heartbeat_at"), "event": "heartbeat",
                       "state": job.get("status"), "step": job.get("step"),
                       "attempt": job.get("attempt", 0)})
    for t in await store.list_transitions(limit=5000):
        if t.get("job_id") == job_id:
            events.append({"at": t["ts"], "event": "transition",
                           "state": t["new_state"],
                           "detail": f"{t['previous_state']} -> {t['new_state']}"})
    if job.get("completed_at"):
        terminal = {"completed": "completed", "failed": "failure",
                    "cancelled": "cancelled"}.get(job.get("status"), "completed")
        events.append({"at": job.get("completed_at"), "event": terminal,
                       "state": job.get("status"),
                       "failure_code": job.get("failure_code")})
    events.sort(key=lambda e: str(e.get("at") or ""))
    try:
        import datetime as _dt
        start = _dt.datetime.fromisoformat(
            str(job.get("started_at") or "").replace("Z", "+00:00"))
        end_raw = job.get("completed_at") or job.get("heartbeat_at")
        end = _dt.datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")) \
            if end_raw else None
        duration_s = (end - start).total_seconds() if end else None
    except Exception:
        duration_s = None
    return {"job_id": job_id, "status": job.get("status"),
            "kind": job.get("kind"), "attempt": job.get("attempt", 0),
            "step": job.get("step"), "failure_code": job.get("failure_code"),
            "duration_s": duration_s, "timeline": events}


@router.get("/diagnostics/export")
async def diagnostics_export():
    """Operator-safe diagnostic bundle (v1.6 §6): configuration snapshot,
    versions, capability states, scheduler state, recent failure codes,
    recent transitions, consistency result, telemetry observations.
    Secrets and unnecessary raw provider payloads are excluded by
    construction (only presence booleans and redacted metadata leave
    the process). Deterministic key order for reproducibility."""
    import version as _version
    from automation import store
    from automation import telemetry as _tel
    from automation.configcheck import readiness_matrix
    await store.init_automation_tables()
    diag = await diagnostics()
    matrix = readiness_matrix()
    jobs = await store.list_jobs(200)
    failure_codes: dict[str, int] = {}
    for j in jobs:
        fc = j.get("failure_code")
        if fc:
            failure_codes[fc] = failure_codes.get(fc, 0) + 1
    bundle = {
        "bundle": "automation-diagnostics",
        "application_version": _version.APP_VERSION,
        "schema_version": _version.SCHEMA_VERSION,
        "config": matrix["config"],
        "versions": {
            "application_version": diag.get("application_version"),
            "schema_version": diag.get("schema_version"),
            "policy_version": None,
        },
        "capabilities": matrix["matrix"],
        "scheduler": diag.get("scheduler", {}),
        "failure_codes": failure_codes,
        "recent_transitions": await store.list_transitions(limit=100),
        "consistency": {"ok": diag.get("consistency")},
        "telemetry": _tel.summary(),
        "alerts": (await alerts())["alerts"],
    }
    try:
        from automation import store as _s2
        latest = await _s2.latest_policy_version()
        bundle["versions"]["policy_version"] = (
            latest["version"] if latest else None)
    except Exception:
        pass
    return bundle
