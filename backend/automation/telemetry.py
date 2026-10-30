"""
telemetry.py — Safe provider-operation telemetry (v1.2 §11, §27).

Records provider/operation/status/retry_after/attempt/duration_ms/
result_state plus stage durations (scan/select/render/reconcile/report).
NEVER records authorization headers, cookies, tokens, or request secrets.
In-memory ring (cap 500) + aggregate counters; exposed via API for the
operational dashboard. No persistence: telemetry is operational, not
evidence (evidence lives in submission attempts / audit log).
"""

from __future__ import annotations

import collections
import threading
import time

_RING: collections.deque = collections.deque(maxlen=500)
_COUNTERS: dict[str, int] = {}
_LOCK = threading.Lock()

_SECRET_HINTS = ("authorization", "cookie", "token", "secret", "password",
                 "api_key", "apikey")


def _safe_meta(meta: dict | None) -> dict:
    clean: dict = {}
    for k, v in (meta or {}).items():
        if any(h in str(k).lower() for h in _SECRET_HINTS):
            clean[k] = "***REDACTED***"
        else:
            clean[k] = v
    return clean


def record(*, provider: str, operation: str, status: str = "ok",
           retry_after=None, attempt: int = 1, duration_ms=None,
           result_state: str = "", meta=None) -> dict:
    event = {"ts": time.time(), "provider": provider, "operation": operation,
             "status": status, "retry_after": retry_after, "attempt": attempt,
             "duration_ms": duration_ms, "result_state": result_state,
             "meta": _safe_meta(meta)}
    with _LOCK:
        _RING.append(event)
        for key in (f"{provider}.{operation}.{status}",
                    f"{provider}.{operation}.count",
                    f"status.{status}"):
            _COUNTERS[key] = _COUNTERS.get(key, 0) + 1
    return event


def summary() -> dict:
    with _LOCK:
        counters = dict(_COUNTERS)
        recent = list(_RING)[-50:]
    agg: dict[str, int] = {"requests": 0, "successes": 0, "count_429": 0,
                           "timeouts": 0, "count_5xx": 0,
                           "unknown_submissions": 0}
    for key, val in counters.items():
        if key.endswith(".count"):
            agg["requests"] += val
    agg["successes"] = sum(v for k, v in counters.items()
                           if k.endswith(".ok"))
    agg["count_429"] = sum(v for k, v in counters.items() if ".429" in k)
    agg["timeouts"] = sum(v for k, v in counters.items() if "timeout" in k)
    agg["count_5xx"] = sum(v for k, v in counters.items()
                           if any(s in k for s in (".500", ".502", ".503", ".504")))
    agg["unknown_submissions"] = sum(
        1 for e in recent if e.get("result_state") == "UNKNOWN_EXTERNAL_RESULT")
    return {"counters": counters, "aggregate": agg, "recent": recent}
