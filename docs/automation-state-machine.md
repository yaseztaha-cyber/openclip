# Automation State Machine (v1.0)

All machines live in `backend/automation/statemachine.py`. Invalid moves
raise `StateTransitionError` → HTTP 409 with
`{"error": {"code": "state_conflict", ...}}`. Every applied transition is
persisted to `automation_transitions` with previous/new state, timestamp,
actor, reason, job/clip/campaign/render ids. Corrupted state is never
silently repaired.

## Campaign
`DISCOVERED → ELIGIBLE | REJECTED | NEEDS_REVIEW | FAILED`;
`NEEDS_REVIEW → ELIGIBLE | REJECTED | FAILED` (explicit owner review);
`ELIGIBLE → APPROVED_FOR_PROCESSING | NEEDS_REVIEW | REJECTED | FAILED`;
`APPROVED_FOR_PROCESSING → PROCESSING → COMPLETED | FAILED`.
`FAILED/REJECTED → DISCOVERED` only via explicit re-scan/override (logged).

## Clip
`DETECTED → FILTERED → SELECTED → RENDERING → RENDERED →
READY_FOR_APPROVAL → APPROVED → PUBLISHING → PUBLISHED` (terminal).
Observation upserts may advance forward along milestones (skips recorded)
but can never enter `APPROVED/PUBLISHING/PUBLISHED` — those come only from
the decision/publish endpoints. Any state may restart at `DETECTED` when a
new render supersedes the old identity (old binding preserved in history).
`REJECTED/PUBLISHED` are terminal; `FAILED → DETECTED` is retry-only.

## Submission
`NOT_SUBMITTED → CLAIMED → SUBMISSION_STARTED → SUBMISSION_ACCEPTED →
AWAITING_CONFIRMATION → CONFIRMED` (terminal); `ALREADY_PUBLISHED`
terminal; `FAILED → SUBMISSION_STARTED` controlled retry only.
`UNKNOWN_EXTERNAL_RESULT` is sticky: only provider-evidence reconciliation
or audited human resolution moves it (`CONFIRMED/FAILED/NEEDS_REVIEW`);
automatic retry from UNKNOWN is refused with 409. See
`docs/automation-provider-reliability.md`.

## Scheduler jobs
`queued → running → completed | failed | needs_review | cancelled`;
`failed → queued` explicit retry only. Publishing jobs are never
auto-rerun after a crash (`require_review`).
