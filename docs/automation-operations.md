# Automation Operations (v1.0)

## Modes
`AUTOMATION_MODE=dry_run` (default, do not change) performs zero external
side effects — enforced at runtime by `side_effects.require_live()`, not
just UI. `live` additionally requires `LIVE_PUBLISHING_ENABLED=true`.

## Publishing (all must hold or the publish is BLOCKED, nothing partial)
live mode, live flag, enabled adapter, credential present, explicit
`APPROVED` approval, unexpired, not invalidated, approval bound to the
exact render/title/description/policy version, verified render artifact,
permitting campaign, unique idempotency key. Single gate:
`publishing.assert_publish_allowed(...)` — used by every publisher path.

## Approvals
Bound to `render_hash + title_hash + description_hash + policy_version`
with `approval_version`, `expires_at` (`APPROVAL_TTL_HOURS`, default 168),
and `invalidated_reason`. A new render supersedes (history preserved);
stale approvals return 409 and can never authorize a different render.

## Scheduler
Resumable steps, bounded retries with exponential backoff, heartbeats +
leases (`SCHEDULER_LEASE_TTL_S`, default 300), stale-job recovery
(resume/retry/mark-failed/require-review; publishing never auto-rerun).
Internal idempotency key `campaign|clip|render|platform` claimed atomically —
single internal claim; external delivery is promised only when the provider
explicitly guarantees it (none currently do).
UNKNOWN submissions are preserved across restarts and reconciled, never
blindly resubmitted. See `docs/automation-provider-reliability.md`.

## Analytics & reports
Observations carry provider/source/status (`CONFIRMED|PENDING|ESTIMATED`);
missing data is `NOT_AVAILABLE`, never zero, never derived into money.
Publication confirmation never implies analytics availability (independent
states). Weekly reports separate confirmed / unknown-publication / failed /
analytics-unavailable; identical content reuses the row; delivered reports
are never resent.

## Analytics & reports
Observations carry provider/source/status (`CONFIRMED|PENDING|ESTIMATED`);
missing data is `NOT_AVAILABLE`, never zero, never derived into money.
Weekly reports are content-hashed: identical content reuses the row;
delivered reports are never resent.

## Failure recovery
Clip failure isolates (campaign continues); ingestion failure marks the
campaign `FAILED` with bounded retry. Every major action emits a
structured event (`automation.events.emit`); transitions persist.
