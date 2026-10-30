# Automation Performance Baseline v2 (v1.6)

Repeated local measurements, Windows dev machine, tmp DB, 2026-09-17.
Milliseconds unless noted. No targets set; no optimization performed.

| Operation | n | min | median | p95 | max |
|---|---|---|---|---|---|
| health | 10 | 160 | 181 | 370 | 370 |
| scan (1 mock campaign) | 10 | 256 | 287 | 570 | 570 |
| select (1 candidate) | 10 | 81 | 99 | 160 | 160 |
| report (weekly) | 10 | 160 | 173 | 222 | 222 |
| diagnostics | 10 | 357 | 404 | 573 | 573 |
| render 9:16 + blur (3s 720p testsrc) | 2 | 1771 | 1794 | 1817 | 1817 |

Earlier single samples: import 3.91s, startup 0.33s (fresh DB).
First-request p95s include one-time model/process warm-up. Render cost is
media-dependent; production figures belong in `/telemetry` aggregates.
