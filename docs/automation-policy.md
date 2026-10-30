# Automation Policy (v1.0)

Precedence (§59): platform → adult → gambling → music → affiliation →
availability → feasibility → score. Rejected campaigns are never ranked.

- Every policy edit creates an immutable version
  (`automation_policies`: id, version, hash, actor, timestamp). The current
  version is served by `GET /api/automation/policy`.
- Every eligibility decision, campaign row, clip-selection output, and
  approval records the `policy_version` used. History is never rewritten.
- `POST /api/automation/policy/re-evaluate` explicitly rescans stored
  campaigns under the current version. UI refreshes never rescan silently.
- Affiliation exclusion matches explicit metadata only
  (`campaign_id/affiliations/brands/entities` vs owner lists). No
  nationality/ethnicity/language/location/religion signals exist in code.
- A policy change after approval blocks publishing (`policy_changed`)
  until re-evaluation + fresh approval.
- Hard exclusions cannot be overridden by scores; owner overrides are
  separate `owner_*` decisions with actor + reason.
