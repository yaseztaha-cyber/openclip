# Automation Testing (v1.0)

- `backend/conftest.py`: runs native `async def` tests via the installed
  `anyio` plugin (asyncio backend). No new dependency, no test rewrites.
- Unit/invariant suites: eligibility (incl. no-proxy test), music
  (ffmpeg-synth fixtures), pipeline (gates/overlap/idempotency/scheduler/
  approvals/reports), people masking (generic-only source scan, tracking,
  parity, real blur render), invariants (15 properties: rejected-never-
  ranked/approved/published, expiry, render binding, dry-run blocks,
  token-less blocks, single-slot idempotency, honest analytics,
  immutable policy history, failure isolation, restart safety,
  deterministic render hash).
- `test_automation_e2e.py`: 34-stage deterministic flow over mock/replay
  providers, mock publisher, mock analytics, no SMTP — asserts rejection,
  ranking, gates, overlap reasons, determinism (repeat select identical),
  real 9:16 + blur renders with ffprobe verification, metadata VALID/
  NEEDS_REVIEW/REJECTED, approval invalidation on re-render, dry-run
  side-effect freedom, single-claim mock-live publish (internal exactly-once
  claim semantics — not external exactly-once delivery), honest report
  (`NOT AVAILABLE` preserved), resend protection, secret sweep, and the
  structured 404 envelope.
- Migrations are additive (`ALTER TABLE ... ADD COLUMN`, ignore-if-exists);
  fresh and existing DBs both initialize cleanly (suite runs against tmp
  DBs; dev DB migrated in place during verification).
