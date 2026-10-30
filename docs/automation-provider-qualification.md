# Automation Provider Qualification (v1.3)

Every capability carries exactly one evidence state. No upgrades without
evidence.

| Capability | Mock | Replay | Authorized (this env) |
|---|---|---|---|
| SUBMIT | MOCK VERIFIED (contract tests) | REPLAY VERIFIED (recorded) | LIVE OPTIONAL (unconfigured here) |
| LOOKUP | MOCK VERIFIED (registry hit/miss) | UNSUPPORTED | UNAVAILABLE (declares no lookup) |
| STATUS | MOCK VERIFIED | UNSUPPORTED | UNAVAILABLE |
| ANALYTICS | UNAVAILABLE (honest) | UNAVAILABLE | UNAVAILABLE |
| HEALTH | MOCK VERIFIED | MOCK VERIFIED | UNAVAILABLE without credential |
| IDEMPOTENCY | Internal VERIFIED; provider UNSUPPORTED | Internal VERIFIED; provider UNSUPPORTED | Internal VERIFIED; provider UNVERIFIED |
| RETRY_AFTER | UNSUPPORTED | UNSUPPORTED | SUPPORTED, UNVERIFIED live |

## Provider records

- Provider: mock / replay — Status: MOCK/REPLAY VERIFIED. Capabilities
  verified by `test_provider_contract.py`. Limitations: simulation only,
  no external I/O. Credentials: none. Read-only checks: N/A (local).
  Live publication checks: none (never applicable).
- Provider: authorized (tiktok) — Status: LIVE OPTIONAL. Verification
  date: never (no authorized test executed here). Verification method:
  read-only `providers/check` + CLI harness (returns UNAVAILABLE without
  credentials, never publishes). Known limitations: upload is an operator
  hook, not a wired integration; no lookup/status/analytics/delete;
  rollback ROLLBACK_UNSUPPORTED. Required credentials:
  `PUBLISH_TOKEN_TIKTOK`, live mode flags, `PUBLISH_PLATFORMS` optional.
- SMTP — Status: UNAVAILABLE here (unconfigured). Qualification ladder:
  NOT_CONFIGURED → AUTHENTICATED (auth-only probe) → SEND_VERIFIED (explicit
  test recipient + delivery ID). `EMAIL_SENT` is never inferred from auth.
- Analytics metrics (views/likes/comments/shares/watch/revenue): each
  NOT_AVAILABLE unless a provider returns it; revenue never derived.

## Rules
- `POST /providers/qualifications` refuses to record LIVE_VERIFIED by hand;
  only authorized flows (read-only check, SMTP test send) write it.
- Upgrading LIVE_OPTIONAL → LIVE_VERIFIED requires a successful
  authorized test (`qualification.upgrade_to_live_verified`, tested).
- Rollback: no adapter supports delete → ROLLBACK_UNSUPPORTED; failures
  never trigger automatic external reversal.

## v1.5 confirmation (2026-09-17, outcome B — no credentials)
Environment verified credential-free (env + `.env` contain paths only).
Pre-live audit: app HEALTHY, DB HEALTHY, ffmpeg HEALTHY, automation READY,
providers honestly UNAVAILABLE, zero unresolved UNKNOWN, consistency clean.
Full suite 171 passed + 1 LIVE-OPTIONAL skip, tsc clean. Per Phase 23 the
run STOPPED before external qualification. Ledger unchanged: mock MOCK
VERIFIED, replay REPLAY VERIFIED, everything external LIVE OPTIONAL or
UNAVAILABLE. No LIVE VERIFIED operations exist.
- Privacy: only operation/reconciliation/audit/reporting data retained;
  raw payloads redacted + truncated (`redact_metadata`); analytics metrics
  redacted on ingest.
