# Automation Operator Acceptance (v1.4)

Acceptance is an explicit recorded event (`POST /api/automation/acceptance`,
also audit-logged as `release_accepted`). The server never marks a release
accepted on its own. This document is the checklist the operator asserts.

## APPLICATION
- [ ] version (`/meta` = expected release, frontend footer matches)
- [ ] startup (lifespan completes, tables migrate, no errors)
- [ ] shutdown (in-flight work recoverable, UNKNOWN stays UNKNOWN)
- [ ] health (`/health` HEALTHY application with optionals UNAVAILABLE)
- [ ] readiness (`/readiness` READY, matrix honest)

## DATA
- [ ] migration (fresh + existing DBs migrate, no loss)
- [ ] consistency (`/consistency` ok:true)
- [ ] backup (file copy / sqlite backup restores with state intact)
- [ ] restore (approvals/attempts/unresolved/reports/policies verified)

## AUTOMATION
- [ ] dry-run (side-effect free, mock simulation only)
- [ ] policy (versioned, re-evaluate works, history immutable)
- [ ] clip selection (deterministic, gates enforced)
- [ ] rendering (9:16 + masks verified by ffprobe)
- [ ] approvals (bound, expiring, supersede on re-render)
- [ ] scheduler (leases, recovery refuses blind publish retry)

## PUBLICATION
- [ ] preflight (READY/BLOCKED, zero side effects, no slot/attempt)
- [ ] explicit confirmation (operator_confirm + hash match enforced)
- [ ] provider status (capabilities explicit per adapter)
- [ ] UNKNOWN handling (sticky, reconcile-first)
- [ ] reconciliation (evidence-based outcomes only)

## SECURITY
- [ ] credential handling (env-only, redacted everywhere, secret sweep clean)
- [ ] logs (structured, redacted, correlated)
- [ ] audit trail (decisions + transitions reconstruct any publication)
- [ ] access controls (token-gated mutations when configured)

## REPORTING
- [ ] analytics (independent, NOT_AVAILABLE preserved)
- [ ] unavailable metrics (never zeroed or monetized)
- [ ] SMTP status (truthful states, no phantom EMAIL_SENT)

## LIVE QUALIFICATION (separate from software acceptance)
- [ ] read-only check performed (published=false)
- [ ] capabilities independently classified
- [ ] controlled publication only if explicitly authorized (record ID)
- [ ] result classified, reconciled where supported, analytics qualified

Software release acceptance MUST NOT be confused with live provider
verification. Current live state: LIVE VERIFICATION NOT PERFORMED
(no credentials or test authorization available in this environment).
