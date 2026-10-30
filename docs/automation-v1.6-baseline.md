# Automation v1.6 Baseline Freeze

Recorded 2026-09-17 before v1.6 changes. Source: actual command output.

- `pytest tests/ --collect-only -q`: **172 tests collected**
- Last full run (v1.5): **171 passed, 1 skipped (LIVE_OPTIONAL), 0 failed**
- `npx tsc --noEmit`: EXIT=0
- `npx eslint` (touched files): 0 errors
- `npm run build`: pass (all routes)
- Release: 1.4.0 / schema 3

Per-file collection (from `--collect-only`):
api_routes 5, audio_signals 8, automation_e2e 1, eligibility 11,
invariants 15, music 5, pipeline 7, caption_ghosting 3, final_editor 1,
frame_parity 1, keyframe 14, lifecycle 11, ops_api 6, ops_lifecycle 6,
parity 1, people_policy 12, provider_contract 15, provider_failures 16,
qualification 16, release_candidate 4, social 9, transform 3, vertical 2.
Sum: 172. No manual calculation beyond this verified listing.
