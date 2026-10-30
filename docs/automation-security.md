# Automation Security (v1.0)

- No secret is hardcoded, logged, stored in plaintext DB fields, returned by
  any endpoint, or embedded in reports/emails/frontend bundles. Verified by
  `test_automation_e2e` secret sweep and repo grep (Phase 17).
- Credentials (`WHOP_API_KEY`, `PUBLISH_TOKEN_*`, `SMTP_*`) come from
  environment variables at call time only. Submission attempt rows store
  redacted provider metadata (`***REDACTED***` for secret-shaped keys,
  500-char error cap, 200-char value truncation) — never bearer tokens.
  User LLM keys keep the existing Fernet-encrypted settings flow.
- Event log (`automation/events.py`) redacts token/secret/password-shaped
  values (`***REDACTED***`).
- Error envelope never includes tracebacks or secrets:
  `{"error": {"code", "message", "details"}}`. Only automation-raised
  `HTTPException(detail={"error": ...})` is unwrapped; all pre-existing
  endpoints keep byte-identical error bodies.
- Prohibited: credential scraping, CAPTCHA/rate-limit bypass, spam, fake
  views/engagement, metric manipulation, private-data scraping.

## Access control (v1.4 audit)
READ (always open): policy/campaigns/approvals/publications/analytics/
reports/jobs/transitions/audit/select/checks/lineage/overview/scheduler
state/telemetry/alerts/readiness/health/liveness/meta/diagnostics/
qualifications/consistency/unresolved/submission-status.
LOCAL_MUTATION, server-gated when `OPERATOR_TOKEN` is set (default
open-local preserves local-first use): policy change, campaign review,
approval decision, submission reconcile/resolve/retry, retention purge,
SMTP verify, qualification recording.
LOCAL_MUTATION, pipeline-local (observation/state-track writes, no
external effect): scan, approval upserts, people review/manual/blur,
scheduler retry/cancel, analytics ingest, report generation.
EXTERNAL_SIDE_EFFECT: publish (operator token + live mode + live flag +
approval + credential + confirmation), SMTP send (live mode + recipients).
Frontend-only checks never substitute: every gate above is enforced in
`automation_api.py` via `require_operator` / `assert_publish_allowed` /
`require_live`, all covered by `test_access_control_classification`.

## Live-operation secrets checklist
`WHOP_API_KEY` (campaigns), `PUBLISH_TOKEN_*` (per platform),
`SMTP_HOST/PORT/USER/PASS/FROM` + `REPORT_RECIPIENTS` (mail). None are
required for local dry-run tests.

## Credential rotation (safe procedure)
1. Create the new credential at the provider; keep the old one valid.
2. Set the new value in the environment (never in source control).
3. Restart/reload the backend (configuration is read at call time — no
   DB migration needed; a failed new credential never erases the old
   configuration evidence).
4. Run `python backend/scripts/provider_check.py --platform <p>` (read-only,
   never publishes) and confirm AUTHENTICATED or better.
5. Revoke the old credential at the provider.
6. Logs, attempts, reports, and diagnostics remain redacted throughout;
   old values are never printed by any diagnostic path (regression-tested
   by `test_ops_lifecycle.test_secret_flow_redaction`).
