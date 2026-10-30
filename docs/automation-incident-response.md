# Automation Incident Response (v1.2)

No destructive automatic recovery is assumed anywhere below. When in doubt,
preserve evidence and escalate to the owner.

## UNKNOWN publication
- Detect: `/submissions/unresolved`, `submission_unknown` alert.
- Contain: slot already claimed — no new external request can be created
  for the key; scheduler refuses blind retry (409 `reconcile_first`).
- Diagnose: inspect attempts (`/submissions/{key}`) for transport class.
- Recover: `POST /submissions/{key}/reconcile`; if NOT_FOUND, controlled
  retry; if STILL_UNKNOWN, human resolve with evidence.
- Verify: state leaves UNRESOLVED only via reconciliation record or
  audited `submission_human_*` decision.

## Duplicate publication suspicion
- Detect: provider 409-existing, duplicate claim, two post URLs one key.
- Contain: do NOT submit again.
- Diagnose: reconcile; compare provider submission IDs in attempt history.
- Recover: mark ALREADY_PUBLISHED with the provider-confirmed URL only.

## Provider outage
- Detect: burst FAILED attempts, `repeated_provider_failures` alert.
- Contain: bounded retry exhausts to FAILED; new work stays local.
- Recover: reconcile when provider returns; NOT_FOUND permits retry.

## Credential failure (401/403)
- NON_RETRYABLE by classification. Rotate per security doc; never resubmit
  with a dead credential. Failed attempts remain as evidence.

## Database corruption
- Detect: health `database: UNHEALTHY`.
- Contain: stop automation writes (app still serves reads where possible).
- Recover: restore from sqlite backup into a FRESH file, run migrations
  (additive, verified by `test_ops_lifecycle`), verify approvals/attempts/
  unresolved intact, then point `DB_PATH` at it. Never repair prod in place.

## Scheduler crash
- On restart, `recover_interrupted`: resume (valid lease), retry (queued),
  mark failed (stale, attempts exhausted), require_review (any publishing
  or UNKNOWN involvement). Verify no `running` rows with ancient heartbeats.

## Render corruption
- `verify_render_artifact` + `_verify_clip_integrity` reject bad outputs;
  publish gate blocks on `render_unverified`. Re-render; the new hash
  supersedes the old approval automatically.

## Report duplication
- Content hash reuses the row; delivered reports are never resent.
  Duplicate `report_id`s for one week indicate a hash-input bug — inspect
  payload inputs before deleting anything.

## Secret exposure
- Revoke at the provider, rotate per security doc, grep logs/DB/reports
  for the value, record the incident in the audit log. The codebase
  redacts secret-shaped values at every boundary (tested).

## Disk exhaustion
- `INSUFFICIENT_STORAGE` (409) before render work; `disk_exhaustion`
  alert. Free space, then run retention cleanup (dry-run first) — only
  unreferenced short-lived files are ever removed.
