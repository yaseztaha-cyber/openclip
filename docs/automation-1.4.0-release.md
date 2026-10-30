# AutoClip 1.4.0 Release

- Release version: 1.4.0 (promoted from 1.4.0-rc1 after recorded operator
  acceptance `acc_f8300cedc5`)
- Schema version: 3 (`automation_meta`; newer-than-code refused loudly)
- Verified tests: 172 collected = 171 passed + 1 skipped (LIVE_OPTIONAL) + 0 failed
- TypeScript: clean · ESLint: 0 errors · production build: pass (all routes)
- Operator acceptance: recorded 2026-09-17 (checklist in acceptance record)

## Provider qualification status
- Mock: MOCK VERIFIED · Replay: REPLAY VERIFIED
- Authorized publisher/lookup/analytics: LIVE OPTIONAL (no authorized test
  executed — no credentials or test authorization available)
- SMTP: UNAVAILABLE here (unconfigured; states truthful)
- External exactly-once delivery: NOT CLAIMED anywhere

## Known unavailable integrations
`WHOP_API_KEY`, `PUBLISH_TOKEN_*`, `SMTP_HOST`, analytics provider.
Application is READY without them; only live publication is gated.

## Known limitations
- Authorized upload is an operator hook, not a wired integration.
- `EMAIL_UNKNOWN` reserved for partial SMTP acceptance.
- Performance figures are single-sample local baselines.
- TestClient-based tests must enter the client context for lifespan
  (documented pattern; enforced by fixture design).

## Operator acceptance
See `docs/automation-operator-acceptance.md` and acceptance record
`acc_f8300cedc5` (`GET /api/automation/acceptance?version=1.4.0-rc1`).
Live provider qualification explicitly NOT performed.

## Rollback procedure
Redeploy the previous build; the database is forward-compatible (additive
migrations only) — verify with `/consistency` after rollback. Destructive
downgrade is unsupported: ROLLBACK_REQUIRES_DATABASE_RESTORE using the
tested sqlite-backup procedure (`test_backup_verifies_*`,
`test_disaster_recovery_simulation`). Frontend/backend versions must match
(compare `/meta` with the UI footer). Post-rollback: health, readiness,
one dry-run scan, unresolved-submission review.
