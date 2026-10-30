"""
statemachine.py — Formalized automation state machines (Phase 1).

Every transition is validated against an explicit allow-list. Invalid
transitions raise StateTransitionError with a structured payload suitable
for a 409 response. Every APPLIED transition returns a history record with
previous_state / new_state / timestamp / actor / reason / job_id / clip_id /
campaign_id / render_id. Corrupted state is never silently repaired.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class StateTransitionError(Exception):
    def __init__(self, machine: str, previous: str | None, attempted: str, reason: str = ""):
        self.machine = machine
        self.previous = previous
        self.attempted = attempted
        super().__init__(f"{machine}: {previous} -> {attempted} not allowed. {reason}")
        self.payload = {"machine": machine, "previous_state": previous,
                        "attempted_state": attempted, "reason": reason or "transition not allowed"}


CAMPAIGN_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"DISCOVERED"},
    "DISCOVERED": {"ELIGIBLE", "REJECTED", "NEEDS_REVIEW", "FAILED"},
    "NEEDS_REVIEW": {"ELIGIBLE", "REJECTED", "FAILED"},  # explicit owner review only
    "ELIGIBLE": {"APPROVED_FOR_PROCESSING", "NEEDS_REVIEW", "REJECTED", "FAILED"},
    "APPROVED_FOR_PROCESSING": {"PROCESSING", "FAILED"},
    "PROCESSING": {"COMPLETED", "FAILED", "NEEDS_REVIEW"},
    "FAILED": {"DISCOVERED"},  # explicit re-scan only
    "REJECTED": {"DISCOVERED"},  # explicit owner override only (logged separately)
    "COMPLETED": set(),
}

CLIP_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"DETECTED"},
    "DETECTED": {"FILTERED", "REJECTED", "FAILED"},
    "FILTERED": {"SELECTED", "REJECTED", "FAILED"},
    "SELECTED": {"RENDERING", "REJECTED", "FAILED"},
    "RENDERING": {"RENDERED", "FAILED"},
    "RENDERED": {"READY_FOR_APPROVAL", "FAILED", "DETECTED"},
    "READY_FOR_APPROVAL": {"APPROVED", "REJECTED", "FAILED", "DETECTED"},
    "APPROVED": {"PUBLISHING", "REJECTED", "FAILED", "DETECTED"},  # DETECTED only: superseded by a new render
    "PUBLISHING": {"PUBLISHED", "FAILED"},
    "PUBLISHED": set(),  # terminal; republishing needs a new clip/render identity
    "REJECTED": set(),
    "FAILED": {"DETECTED"},  # explicit retry only
}

# Canonical pipeline milestones in order. Observation upserts may move
# FORWARD along this order (recording skipped milestones) but may never
# jump into APPROVED/PUBLISHING/PUBLISHED — those are reachable only via
# the decision/publish endpoints. Backward moves are allowed only onto
# DETECTED (a new render restarts the clip machine).
CLIP_MILESTONE_ORDER = ["DETECTED", "FILTERED", "SELECTED", "RENDERING",
                        "RENDERED", "READY_FOR_APPROVAL"]
CLIP_IMPORTABLE = set(CLIP_MILESTONE_ORDER)  # first observation of a clip


def advance_clip_observation(previous: str | None, target: str) -> tuple[str, str]:
    """
    Validate a pipeline observation upsert. Returns (effective_state, note).
    Raises StateTransitionError on any disallowed move.
    """
    if target in ("APPROVED", "PUBLISHING", "PUBLISHED"):
        raise StateTransitionError("clip", previous, target,
                                   "approvals/publishing only via decision/publish endpoints")
    if previous is None:
        if target not in CLIP_IMPORTABLE:
            raise StateTransitionError("clip", previous, target,
                                       "first observation must be a pipeline milestone")
        return target, "observed_import"
    if target == previous:
        return target, "no-op"
    if target == "DETECTED":
        # New render / explicit restart from a non-terminal state.
        if previous in ("PUBLISHED", "REJECTED"):
            raise StateTransitionError("clip", previous, target, "terminal state")
        skipped: list[str] = []
        return target, "restart_new_render"
    if previous in CLIP_MILESTONE_ORDER and target in CLIP_MILESTONE_ORDER:
        if CLIP_MILESTONE_ORDER.index(target) > CLIP_MILESTONE_ORDER.index(previous):
            skipped = CLIP_MILESTONE_ORDER[CLIP_MILESTONE_ORDER.index(previous) + 1:
                                           CLIP_MILESTONE_ORDER.index(target)]
            note = "observed_import" if not skipped else f"advanced_skipped_{'+'.join(skipped)}"
            return target, note
        raise StateTransitionError("clip", previous, target, "backward move not allowed")
    # Terminal failure/rejection observations from milestones.
    if target in ("REJECTED", "FAILED") and previous in CLIP_MILESTONE_ORDER:
        return target, "observed_terminal"
    raise StateTransitionError("clip", previous, target, "transition not allowed")

SUBMISSION_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"NOT_SUBMITTED"},
    "NOT_SUBMITTED": {"CLAIMED", "ALREADY_PUBLISHED"},
    "CLAIMED": {"SUBMISSION_STARTED", "FAILED", "ALREADY_PUBLISHED"},
    "SUBMISSION_STARTED": {"SUBMISSION_ACCEPTED", "AWAITING_CONFIRMATION",
                           "FAILED", "UNKNOWN_EXTERNAL_RESULT",
                           "ALREADY_PUBLISHED"},
    "SUBMISSION_ACCEPTED": {"AWAITING_CONFIRMATION", "CONFIRMED", "FAILED",
                            "UNKNOWN_EXTERNAL_RESULT"},
    "AWAITING_CONFIRMATION": {"CONFIRMED", "FAILED", "UNKNOWN_EXTERNAL_RESULT",
                              "NEEDS_REVIEW"},
    # UNKNOWN is sticky: only reconciliation evidence or audited human
    # resolution may move it. NEVER an automatic retry, NEVER silent FAILED.
    "UNKNOWN_EXTERNAL_RESULT": {"CONFIRMED", "FAILED", "NEEDS_REVIEW"},
    "NEEDS_REVIEW": {"CONFIRMED", "FAILED", "SUBMISSION_STARTED"},
    "CONFIRMED": set(),
    "FAILED": {"SUBMISSION_STARTED"},  # controlled retry only (never from UNKNOWN)
    "ALREADY_PUBLISHED": set(),
}

JOB_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"queued"},
    "queued": {"running", "failed"},
    "running": {"completed", "failed", "needs_review", "cancelled"},
    "needs_review": {"running", "failed", "cancelled"},
    "completed": set(),
    "failed": {"queued"},  # explicit retry only
    "cancelled": set(),
}


@dataclass(frozen=True)
class TransitionRecord:
    machine: str
    previous_state: str | None
    new_state: str
    timestamp: str
    actor: str
    reason: str
    job_id: str | None = None
    clip_id: str | None = None
    campaign_id: str | None = None
    render_id: str | None = None


def validate(machine: dict[str | None, set[str]], name: str,
             previous: str | None, new_state: str) -> None:
    allowed = machine.get(previous, set())
    if new_state not in allowed:
        raise StateTransitionError(name, previous, new_state)


def transition(machine: dict[str | None, set[str]], name: str,
               previous: str | None, new_state: str, *, actor: str = "automation",
               reason: str = "", job_id=None, clip_id=None,
               campaign_id=None, render_id=None) -> TransitionRecord:
    validate(machine, name, previous, new_state)
    return TransitionRecord(machine=name, previous_state=previous, new_state=new_state,
                            timestamp=_now(), actor=actor, reason=reason,
                            job_id=job_id, clip_id=clip_id,
                            campaign_id=campaign_id, render_id=render_id)
