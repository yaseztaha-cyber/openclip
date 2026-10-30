# Automation Deployment (v1.4)

## Environments
DEVELOPMENT (local, open-local auth) · TEST (tmp DBs, no creds) · DRY_RUN
(default Automation Hub mode) · MOCK / REPLAY (simulators) · LIVE
(explicit flags + credentials + approvals).

Precedence: process environment > `.env` file > built-in safe defaults.
TEST/DRY_RUN never require live credentials. DEVELOPMENT never inherits
production credentials: no `.env` is loaded from outside the project dir,
and `database.py` only reads `DB_PATH`/`TMP_DIR` (paths, never secrets).

## Deploy steps
1. Provision host with FFmpeg + Python 3.14 + Node 20.
2. Backend: `pip install -r backend/requirements.lock`.
3. Frontend: `npm ci --prefix frontend` (deterministic via package-lock).
4. Configure env from `backend/.env.example` (paths only for dry-run;
   tokens only for LIVE).
5. Start backend (`uvicorn api:app`, cwd `backend/`), then frontend
   (`npm run build && npm run start --prefix frontend` or `npm run dev`).
6. Verify: `/api/automation/liveness` → alive; `/readiness` → READY.

## What NEVER happens automatically
No publishing at startup, no scheduler-triggered live publish, no health
check publishing, no CI live tests. Live publish needs operator_confirm +
approval + credentials every time.
