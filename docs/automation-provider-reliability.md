# Automation Provider Reliability (v1.1)

## Guarantee boundary (read carefully)

- **Internal exactly-once CLAIM semantics**: the `automation_publications`
  row for an idempotency key (`campaign|clip|render|platform`, canonicalized
  then hashed) is claimed atomically. Double-click, retry, concurrent
  workers, and process restart yield exactly one internal claim. Tested.
- **Idempotency-protected external submission**: retries reuse the same key
  and are reconciled against provider evidence; blind duplicate requests
  are structurally impossible (slot claim + UNKNOWN-must-reconcile-first).
- **External exactly-once delivery is NEVER claimed** unless a provider
  explicitly guarantees it. No current provider does.

## UNKNOWN_EXTERNAL_RESULT

Timeout, connection reset, lost/unparsable response after possible
transmission → `UNKNOWN_EXTERNAL_RESULT`, never FAILED, never auto-retry.
Unknown submissions survive scheduler restarts and can only leave via
provider-evidence reconciliation or audited human resolution.

## Reconciliation (`automation/submissions.py`)

`PublicationReconciliationService` (`reconcile_one`): finds unresolved
submissions, queries the adapter lookup when supported, matches on
provider submission ID / platform object ID / idempotency key. Outcomes:
`CONFIRMED` (only with provider evidence incl. post URL),
`NOT_FOUND` (controlled retry permitted),
`FAILED`, `STILL_UNKNOWN` (lookup unsupported or inconclusive),
`NEEDS_REVIEW`. Attempts are append-only; confirmation is never fabricated.

## Retry matrix

- NON_RETRYABLE (400/401/403/404-invalid/409-existing/validation/policy/
  stale approval) → FAILED, human resolution only.
- RETRYABLE (429/500/502/503/504) → bounded attempts (`MAX_ATTEMPTS=4`),
  `Retry-After` honored, else exponential backoff + jitter (cap 300s).
- AMBIGUOUS (any transport doubt) → UNKNOWN, reconcile first.
- No rate-limit bypass, credential rotation, CAPTCHA bypass, or stealth
  requests — ever.

## Capability model

Adapters declare `supports_submission_lookup/idempotency_keys/
submission_status/delete/analytics/retry_after`. Status endpoint reports
`SUPPORTED → CONFIGURED → AUTHENTICATED → AVAILABLE → OPERATIONAL`, plus
`UNAVAILABLE`. LIVE never falls back to MOCK/REPLAY; missing credentials
are UNAVAILABLE, never fake success.

## Scheduler recovery

Crash mid-publish → `require_review`. UNKNOWN payloads are preserved and
reconciled, never re-submitted blindly. Heartbeats/leases detect stale
workers (`SCHEDULER_LEASE_TTL_S`).

## Capability states in this repo (honest labels)

| Capability | State |
|---|---|
| Mock publisher/analytics/report mail (no SMTP) | MOCK |
| Replay provider/publisher | REPLAY |
| Authorized publisher upload | LIVE OPTIONAL (operator hook; needs token + live mode) |
| Whop official campaigns | UNAVAILABLE without `WHOP_API_KEY` (no scraping fallback) |
| SMTP delivery | UNAVAILABLE without `SMTP_HOST` (stored `EMAIL NOT SENT`) |
| Submission lookup (authorized) | UNAVAILABLE (adapter default; mock supports in-memory lookup) |
