# Automation Maintenance (v1.6)

Monthly or on suspicion; never automatic.

- Dependencies: review `requirements.lock` vs `requirements.txt`; update
  only with a full suite re-run. Never auto-update. Frontend: `npm ci`
  stays lockfile-pinned; audit with `npm audit` on change.
- DB migrations: additive only; verify fresh + existing DBs; never
  downgrade (`IncompatibleSchema` enforced).
- Fixtures: `backend/tests/fixtures/*` carry id/provider/version/
  created_at/schema/description; no credentials, no personal data.
- Provider qualification: re-run read-only checks after credential
  rotation; record outcomes; never hand-assert LIVE_VERIFIED.
- Security scans: repo grep (CI), diagnostics/export redaction test,
  report/DB/E2E artifact sweeps, frontend bundle has no secret values
  (only `NEXT_PUBLIC_API_URL` is public).
- Log retention: uvicorn/backend logs are operational, not evidence;
  evidence lives in attempts/transitions/decisions (LONG-LIVED, never
  collected). Rotate logs freely.
- Artifact cleanup: `POST /retention/cleanup` dry-run first; only
  unreferenced SHORT-LIVED files are removed, every deletion audited.
- Report cleanup: reports are DB rows; keep history for trends; delete
  only whole weeks the owner no longer needs.
- Backup verification: restore into a FRESH file quarterly and run
  `/consistency` (see disaster-recovery doc).
- Release procedure: `docs/automation-release-runbook.md`; version bump
  only in `backend/version.py`; snapshot in `docs/release-snapshot.json`.
