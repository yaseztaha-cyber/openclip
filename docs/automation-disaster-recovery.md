# Automation Disaster Recovery (v1.4)

## Backup
SQLite file copy or `Connection.backup()` while running. Verify with
`test_backup_verifies_policies_qualifications_reports` (policies,
qualifications, reports, campaigns, approvals, attempts, decisions).

## Restore
Restore into a FRESH file, run migrations (additive), check
`ensure_schema_compatible()` (fails loudly if newer), point `DB_PATH` at
it. Never downgrade: newer-schema DBs are rejected, never mutated.

## Simulated matrix (tested in `test_disaster_recovery_simulation`)
DB restored + artifact missing + scheduler restarted + UNKNOWN present →
app starts, missing artifact surfaced (not hidden), safe work resumes,
UNKNOWN stays unresolved, duplicate publish impossible (slot held).

## Artifact classes
SOURCE (downloads) · CACHE (transcripts) · TEMPORARY (segments/staging,
short-lived, reference-checked cleanup) · RENDER (clip outputs, bound to
approvals) · REPORT (DB-persisted HTML) · EVIDENCE (attempts/audit/policy,
never collected). Paths derive from `TMP_DIR`; no absolute production
paths; Windows dev paths preserved via env configuration.
