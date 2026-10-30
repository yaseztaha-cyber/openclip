# Automation Live Provider Profile (v1.5)

Template + current values. No secrets are recorded here — only presence,
endpoints, and mechanisms.

## Authorized publishing adapter (`authorized`)
- Provider: operator-configured platform API (per-platform)
- Platform: tiktok / youtube (via `PUBLISH_PLATFORMS` allow-list)
- Adapter: `AuthorizedPlatformPublisher` + `AuthorizedProviderClient`
- Client version: v1 (`automation/provider_client.py`)
- API/base URL: operator-provided (no default; no scraping fallback)
- Authentication mechanism: bearer/token from `PUBLISH_TOKEN_<PLATFORM>`
  (env only, read at call time)
- Required credentials: `PUBLISH_TOKEN_<PLATFORM>`; live mode flags
- Required permissions: platform upload + read (operator grants)
- Supported operations: submit (operator hook — concrete upload call is
  the operator's wiring point)
- Unsupported operations: lookup, status, analytics, delete, replay
- Rate limits: provider-defined; `Retry-After` honored, else bounded
  exponential backoff + jitter (cap 300s, budget `MAX_ATTEMPTS=4`)
- Idempotency support: UNSUPPORTED at provider layer (internal atomic
  claim is separate and VERIFIED)
- Lookup support: none declared (UNKNOWN → STILL_UNKNOWN → human review)
- Analytics support: none declared (all metrics NOT_AVAILABLE)
- Rollback support: ROLLBACK_UNSUPPORTED (no delete path exists)
- Known limitations: upload hook unwired; no lookup/status/analytics;
  ambiguous transport outcomes stay UNKNOWN by design

## Campaign discovery (`whop_official`)
- API/base URL: `WHOP_API_BASE` (default `https://api.whop.com/api/v1`,
  per Current API docs; version pinned via `WHOP_API_VERSION_DATE`)
- Authentication: `WHOP_API_KEY` bearer (env only); read-only validation
  via `GET /api/v1/accounts/me` (no other call needed to prove auth)
- Supported: authorized discovery only; no scraping path exists
- Operator-declared target account: `biz_qJ4GR20emzKKYr`
  (set `WHOP_ACCOUNT_ID` to it; any other authenticated account refuses)
- Campaign mapping: UNMAPPED — Whop exposes products/plans/memberships,
  not campaigns. Discovery stays disabled until the operator maps
  `WHOP_CAMPAIGNS_PATH` and verifies normalization. No `/campaigns`
  endpoint is assumed.
- Network path verified 2026-09-17: unauthenticated
  `GET /api/v1/accounts/me` returns HTTP 401 (reachable, auth required;
  no credentials used). Auth itself: UNAVAILABLE (no key configured).

## SMTP
- Authentication: host/port + optional login (env only)
- Controlled verification: auth-only probe, or explicit test-recipient
  send with delivery ID. Production reports never sent during qualification.

## Current qualification state (2026-09-17, no credentials present)
SUBMIT/ECOSYSTEM: mock MOCK VERIFIED, replay REPLAY VERIFIED, authorized
LIVE OPTIONAL. No LIVE VERIFIED operations exist.
