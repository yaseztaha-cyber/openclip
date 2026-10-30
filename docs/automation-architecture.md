# Automation Architecture (v1.0)

Orchestration layer on top of the existing AutoClip systems. Nothing in the
detection / scoring / render path was rewritten; the hub calls it.

```
CampaignProvider (mock | replay | whop_official)
  → normalize → CampaignEligibilityEngine (policy vN)
  → audit + persist → rank eligible only
  → ingest gate → candidates (existing llm/scoring/signals/editorial)
  → silence/music/people gates → multimodal score → strongest-overlap
  → diversity → social render (existing reframer/composition/captioner)
  → people-mask FFmpeg layer → title/description → approval queue
  → centralized publish gate → idempotent publish → analytics → report
```

Status of every externally visible behavior:

| Behavior | Status |
|---|---|
| Campaign discovery (mock/replay) | REAL (local) |
| Whop provider | REAL code, UNAVAILABLE without `WHOP_API_KEY` (returns `[]`, never scrapes) |
| Eligibility / ranking / selection | REAL |
| Social render / captions | REAL (existing pipeline) |
| People-mask render | REAL (FFmpeg, render-time detection) |
| Mock publisher | MOCK (dry-run simulation only) |
| Authorized publisher | REAL gate, operator upload hook (no auto-upload claimed) |
| Submission lookup (authorized) | UNAVAILABLE by default (adapter declares no lookup); mock supports in-memory lookup |
| External exactly-once delivery | NOT CLAIMED (internal exactly-once claim only; see provider-reliability doc) |
| Analytics | REAL where provider returns data, else NOT_AVAILABLE |
| Weekly email | REAL via SMTP when configured, else EMAIL NOT SENT |
| Publication success | claimed only on platform confirmation |

Key modules: `backend/automation/` (`policy`, `eligibility`, `music`,
`providers`, `ranking`, `titles`, `store`, `statemachine`, `side_effects`,
`scheduler`, `publishing`, `analytics`, `reports`, `people_policy`,
`pipeline`, `render_identity`, `events`); routes in
`backend/automation_api.py` under `/api/automation`.
