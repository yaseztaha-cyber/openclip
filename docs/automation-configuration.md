# Automation Configuration (v1.4)

Authoritative source: `backend/.env.example`. Runtime snapshot (sanitized):
`GET /api/automation/diagnostics/config`. Validation:
`GET /api/automation/readiness`.

| Variable | Default | Production requirement |
|---|---|---|
| AUTOMATION_MODE | dry_run | `live` only for real operation |
| LIVE_PUBLISHING_ENABLED | false | `true` for any live publish |
| HUMAN_APPROVAL_REQUIRED | true | keep `true` |
| OPERATOR_TOKEN | (unset = open-local) | set to gate mutations server-side |
| PUBLISH_TOKEN_* | unset | required per live platform |
| PUBLISH_PLATFORMS | (any) | allow-list for live |
| WHOP_API_KEY / BASE | unset | required for official discovery |
| SMTP_* / REPORT_RECIPIENTS | unset | required for mail (optional) |
| APPROVAL_TTL_HOURS | 168 | tune per policy |
| MAX_ATTEMPTS | 4 | bounded retry budget |
| EDITOR_TRANSCRIPT_TIMEOUT | 90 | interactive budget |
| SCHEDULER_LEASE_TTL_S / HEARTBEAT_S | 300 / 15 | worker lease |
| MIN_FREE_DISK_BYTES | 524288000 | render guard |
| TMP_DIR / DB_PATH | repo-local | persistent volumes in prod |
| CORS_ORIGIN | localhost:3000 | frontend origin |

No credential is required for local dry-run tests. Secrets live only in
process env, read at call time, never logged/stored/returned.

Operator-token note: the browser dashboard operates in open-local mode
(browser-held tokens would put secrets into frontend state, which this
project forbids). Token-gated deployments are driven by API clients that
send `X-Operator-Token`; the UI surfaces 401s honestly via parseErr.
