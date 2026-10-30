# Automation Data Consistency (v1.4)

`GET /api/automation/consistency` returns structured integrity errors
without mutating anything:

- approval → clip exists; approval carries render_hash + policy_version
- submission → clip exists; CONFIRMED always has a post URL
- latest transition per entity agrees with current state
- qualifications reference known providers
- approval preview artifacts resolve on disk

Write-path audit: every mutation uses a short-lived connection with an
explicit commit; API success is returned only after commit (verified by
read-back tests across approvals/submissions/transitions/jobs/reports).
`claim_publication_slot` is a single atomic INSERT; retries/duplicate
clicks/concurrent workers converge on one row.
