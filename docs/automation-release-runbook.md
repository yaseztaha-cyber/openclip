# Automation Release Runbook — 1.4.0-rc1

## Pre-release checklist
- [ ] `pytest backend/tests/ -q` green (expect 156+ passed, ≤1 LIVE-OPTIONAL skip, 0 failed)
- [ ] `npx tsc --noEmit` clean, `npx eslint` 0 errors, `npm run build` succeeds
- [ ] No `LIVE VERIFIED` claims without authorized evidence (`/providers/qualifications`)
- [ ] CHANGELOGVersions: `backend/version.py` (`APP_VERSION`, `SCHEMA_VERSION`)

## Backup
Copy `data/openclip.db` (or sqlite `backup()`); record path + checksum.

## Migration
Deploy code; on boot, additive migrations run; `ensure_schema_compatible`
fails loudly on newer-than-code schema (do not bypass).

## Deployment
Backend (`requirements.lock`, `uvicorn api:app`), frontend (`npm ci`,
`npm run build`, `npm start`). Env from `.env.example`.

## Health check
`/liveness` alive → `/readiness` READY → `/health` deps →
`/consistency` ok:true → spot-check automation dashboard.

## Verification
Dry-run scan + preflight BLOCKED-without-approval + weekly report
(`EMAIL_DISABLED` without SMTP) + unresolved list empty or triaged.

## Rollback
1. Stop services. 2. Redeploy previous version. 3. Database: roll back
   ONLY if previous code supports the current schema version; otherwise
   keep the DB (additive migrations are forward-compatible) and verify
   with `/consistency`. 4. Never downgrade destructively without the
   tested backup/restore procedure above. 5. Frontend/backend versions
   must match (`/meta` on both sides). 6. Post-rollback: repeat health
   check + one dry-run scan.

## Post-rollback verification
Same as health check, plus confirm no UNKNOWN submission changed state
during the window (transitions log).
