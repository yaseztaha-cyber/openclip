# Automation Error Catalog (v1.6)

Every entry below corresponds to a code the system actually emits. No two
entries are semantically equivalent; envelope shape is always
`{"error": {"code", "message", "details"}}`.

## Policy / eligibility
| Code | HTTP | Meaning | Retryable | Operator action | Auto recovery |
|---|---|---|---|---|---|
| POLICY_BLOCKED | — (decision) | hard exclusion hit (adult/gambling/music/affiliation) | no | review evidence, narrow policy or reject | no |
| POLICY_REVIEW_REQUIRED | — (decision) | uncertain → NEEDS_REVIEW | no | manual review campaign/track | no |

## Approvals
| Code | HTTP | Meaning | Retryable | Operator action | Auto recovery |
|---|---|---|---|---|---|
| stale_approval_view | 409 | render changed since view loaded | no | re-approve current render | no |
| approval_expired | 409 | approval past `expires_at` | no | re-approve | no |
| approval_invalidated | 409 | superseded/invalidated | no | re-approve | no |
| render_mismatch | 409 | approval bound to another render | no | approve current render | no |
| metadata_mismatch | 409 | title/description changed post-approval | no | re-approve | no |
| policy_changed | 409 | policy version drifted post-approval | no | re-evaluate + re-approve | no |
| platform_mismatch | 409 | approval bound to another platform | no | re-approve per platform | no |
| state_conflict | 409 | invalid state-machine transition | no | follow allowed transitions | no |

## Publishing / submissions
| Code | HTTP | Meaning | Retryable | Operator action | Auto recovery |
|---|---|---|---|---|---|
| dry_run / dry_run_blocked | 403 | external action attempted in dry-run | no | switch mode explicitly if intended | no |
| live_disabled | 409/403 | live flag off | no | set flags deliberately | no |
| no_approval / not_approved | 409 | missing/non-APPROVED approval | no | approve | no |
| missing_credential | 409 | no token configured | no | configure credential | no |
| adapter_disabled / adapter_unavailable | 409 | adapter not usable | no | configure/wire adapter | no |
| confirmation_required | 403 | live publish without operator confirmation | no | confirm explicitly | no |
| render_unverified | 409 | artifact failed ffprobe checks | no | re-render | no |
| duplicate | 409 | idempotency key already claimed | no | inspect existing submission | no |
| reconcile_first | 409 | retry attempted on UNKNOWN | no | reconcile, then controlled retry | no |
| retry_not_permitted | 409 | non-retryable failure | no | manual resolution | no |
| campaign_not_permitted | 409 | campaign not eligible | no | re-evaluate campaign | no |

## Providers / transport
| Code | HTTP | Meaning | Retryable | Operator action | Auto recovery |
|---|---|---|---|---|---|
| PROVIDER_UNAVAILABLE | — (status) | adapter has no credential/transport | no | configure | no |
| PROVIDER_TIMEOUT | — (attempt) | timeout after possible transmission → UNKNOWN | reconcile only | reconcile | no |
| SUBMISSION_UNKNOWN | — (state) | result unknowable locally | reconcile only | reconcile/review | no |
| RATE_LIMITED | — (429) | provider throttling | bounded (Retry-After) | wait, reduce rate | scheduled retry |

## Jobs / scheduler
| Code | HTTP | Meaning | Retryable | Operator action | Auto recovery |
|---|---|---|---|---|---|
| JOB_LEASE_CONFLICT | — | stale lease, another worker may own it | review | require_review on republish paths | resume/retry/fail per recovery |
| cancelled | — | operator/server cancelled | no | inspect, requeue manually | no |

## Storage / schema
| Code | HTTP | Meaning | Retryable | Operator action | Auto recovery |
|---|---|---|---|---|---|
| INSUFFICIENT_STORAGE | 409 | disk below `MIN_FREE_DISK_BYTES` | after freeing | free space, retention cleanup | no |
| INCOMPATIBLE_SCHEMA | startup | DB newer than code | no | deploy matching build, never downgrade | no |
| CONSISTENCY_FAILURE | — (report) | integrity errors listed | no | fix data, re-run check | no |

## Auth / misc
| Code | HTTP | Meaning | Retryable | Operator action | Auto recovery |
|---|---|---|---|---|---|
| unauthorized | 401 | bad/missing `X-Operator-Token` | no | present token | no |
| invalid_request | 400 | malformed request | after fix | fix request | no |
| not_found | 404 | unknown id/key | no | check identifier | no |
| no_blur_tracks | 400 | blur render with no BLUR tracks | no | review tracks first | no |
| internal_error | 500 | unexpected failure (no tracebacks leaked) | inspect | report with request_id | no |
