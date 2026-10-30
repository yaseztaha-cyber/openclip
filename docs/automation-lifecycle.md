# Automation Lifecycle (v1.4)

Single `lifespan()` in `backend/api.py` (modern FastAPI; `@app.on_event`
removed). Router + exception handlers register at import time (middleware
snapshots handlers at stack build — startup registration is silently
ignored, see v1.1 findings).

Startup (idempotent, local-only, no external side effects):
1. `database.init_db()` (core tables, `IF NOT EXISTS`)
2. TMP dir ensure, concurrency semaphores
3. stale-project reset, orphaned-render-job recovery
4. `init_automation_tables()` (additive migrations) + schema-compat check
5. orphaned temp-file sweep

Shutdown: sets `_SHUTTING_DOWN`, logs; in-flight editor renders already
handle `CancelledError` → `cancelled`; everything else recovers next boot
via orphaned-job + stale-lease recovery. UNKNOWN submissions stay UNKNOWN;
nothing is silently repeated or converted to FAILED.

Idempotency proof: `test_lifespan_start_stop_idempotent` enters/exits the
app 3× (routes stable, tables intact, limiters rebuilt). Fixtures use
`with TestClient(...)` so lifespan runs — no manual `init_db()` crutch.
