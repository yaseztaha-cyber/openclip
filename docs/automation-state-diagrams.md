# Automation State Machines (visual, v1.6)

Terminal states are marked `[*]`. Recovery edges are labeled. There is no
automatic recovery where none is drawn.

## Campaign
```
          ┌──────────┐
          │DISCOVERED│
          └────┬─────┘
     ┌─────────┼───────────────┐
     ▼         ▼               ▼
 ELIGIBLE  NEEDS_REVIEW     REJECTED
     │      (owner        (owner override
     │       review)       → DISCOVERED)
     ▼         │ ▲
 APPROVED_FOR_PROCESSING
     │
     ▼
 PROCESSING ──► COMPLETED [*]
     │  ▲
     └──┴──► FAILED ──► DISCOVERED (explicit re-scan only)
```
Invalid: COMPLETED→anything, REJECTED→ELIGIBLE without owner review,
any skip of the order.

## Clip
```
DETECTED → FILTERED → SELECTED → RENDERING → RENDERED → READY_FOR_APPROVAL
                                                             │
                              ┌──────────────────────────────┤
                              ▼                              ▼
                          APPROVED ──► PUBLISHING ──► PUBLISHED [*]
                              │            │
REJECTED [*] ◄────────────────┴────────────┴── FAILED ──► DETECTED (retry only)
   ▲
   │ any milestone may restart at DETECTED on a new render
   │ (old binding preserved in history; old approval never transfers)
```
Invalid: direct creation of APPROVED/PUBLISHING/PUBLISHED (decision and
publish endpoints only), backward moves except →DETECTED, PUBLISHED→*.

## Submission
```
NOT_SUBMITTED → CLAIMED → SUBMISSION_STARTED → SUBMISSION_ACCEPTED
                                              → AWAITING_CONFIRMATION
                                                      │
                    ┌─────────────────────────────────┼──────────────────┐
                    ▼                                 ▼                  ▼
               CONFIRMED [*]                     FAILED ──► SUBMISSION_STARTED
                 (URL required)                  (controlled retry only;
                                                  never from UNKNOWN)
UNKNOWN_EXTERNAL_RESULT ◄── timeout/reset/lost-response after transmission
   │  (FIRST-CLASS STATE: sticky)
   ├──► CONFIRMED (provider evidence + URL)
   ├──► FAILED (provider evidence of failure)
   └──► NEEDS_REVIEW (inconclusive / human resolution)
ALREADY_PUBLISHED [*] (duplicate key: recover existing, never resubmit blind)
```
No edge UNKNOWN→retry exists. No edge converts UNKNOWN→FAILED silently.

## Job
```
queued → running → completed [*]
              ├─→ failed ──► queued (explicit retry only; never for publish/UNKNOWN)
              ├─→ needs_review ──► running/failed/cancelled
              └─→ cancelled [*]
```
Crash recovery: resume (valid lease) / retry (queued) / mark failed
(stale, budget spent) / require_review (publishing or UNKNOWN involved).
