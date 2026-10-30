"""Phase-10/14 failure-injection tests (A–L): deterministic scripted-provider.

Every scenario runs against a temporary DB with scripted fake adapters
implementing the Publisher interface. No network, no credentials.
"""
import asyncio
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from automation import store
from automation.publishing import (Publisher, MockPublisher, build_idem_key,
                                   canonical_idem_key, resolve_publisher,
                                   PublishBlocked, adapter_status)
from automation.scheduler import AutomationScheduler
from automation.submissions import (classify_provider_error, reconcile_one,
                                    find_unresolved, retry_delay_s,
                                    LookupUnavailable, NON_RETRYABLE,
                                    RETRYABLE, AMBIGUOUS,
                                    UNKNOWN_EXTERNAL_RESULT, MAX_ATTEMPTS)


def _use_tmp_db(tmp_path, name="failinj.db"):
    database.DB_PATH = str(tmp_path / name)
    asyncio.run(store.init_automation_tables())


class ScriptedPublisher(Publisher):
    """Test double: replays a scripted outcome sequence per idempotency key."""

    name = "scripted"
    external = True
    CAPABILITIES = {
        "supports_submission_lookup": True,
        "supports_idempotency_keys": True,
        "supports_submission_status": True,
        "supports_delete": False,
        "supports_analytics": False,
        "supports_retry_after": True,
    }

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self._registry = {}

    def publish(self, clip_path, title, description, platform, idem_key, **kw):
        self.calls += 1
        if idem_key in self._registry:
            return {**self._registry[idem_key], "duplicate": True}
        action = self.script[min(self.calls - 1, len(self.script) - 1)]
        kind = action[0]
        if kind == "raise":
            raise action[1]
        rec = {"post_url": f"scripted://{platform}/{idem_key[:8]}",
               "platform": platform, "status": action[1],
               "provider_status_code": action[2] if len(action) > 2 else None,
               "provider_submission_id": f"sub_{idem_key[:8]}",
               "retry_after": action[3] if len(action) > 3 else None}
        self._registry[idem_key] = rec
        return rec

    def lookup_submission(self, *, idempotency_key=None,
                          provider_submission_id=None):
        for k, rec in self._registry.items():
            if k == idempotency_key or rec.get("provider_submission_id") == provider_submission_id:
                if rec.get("status") == "published":
                    return {"status": "CONFIRMED", "post_url": rec["post_url"],
                            "provider_submission_id": rec["provider_submission_id"]}
                return {"status": rec.get("status", "").upper(),
                        "provider_submission_id": rec.get("provider_submission_id")}
        return None


async def _submit(store_mod, pub, key, **kw):
    """Drive one live-path submission attempt through the real primitives."""
    claimed = await store_mod.claim_publication_slot("clip1", "camp1", "tiktok", key)
    if not claimed:
        return {"status": "ALREADY_PUBLISHED"}
    attempt = await store_mod.record_submission_attempt(
        campaign_id="camp1", clip_id="clip1", platform="tiktok",
        idempotency_key=key, request_status="STARTED")
    try:
        result = pub.publish("c.mp4", "t", "d", "tiktok", key)
    except (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
        await store_mod.finish_submission_attempt(
            attempt["submission_attempt_id"], request_status="TRANSPORT_AMBIGUOUS",
            result_state=UNKNOWN_EXTERNAL_RESULT, error_code=type(exc).__name__)
        await store_mod.update_publication_by_key(key, status="unknown",
                                                  submission_state=UNKNOWN_EXTERNAL_RESULT)
        return {"status": UNKNOWN_EXTERNAL_RESULT,
                "submission_attempt_id": attempt["submission_attempt_id"]}
    code = result.get("provider_status_code")
    verdict = classify_provider_error(code)
    if result.get("status") == "published" and result.get("post_url"):
        await store_mod.finish_submission_attempt(
            attempt["submission_attempt_id"], request_status="RESPONDED",
            provider_submission_id=result.get("provider_submission_id"),
            result_state="CONFIRMED")
        await store_mod.update_publication_by_key(key, status="published",
                                                  post_url=result["post_url"],
                                                  submission_state="CONFIRMED")
        return {"status": "published", "post_url": result["post_url"]}
    await store_mod.finish_submission_attempt(
        attempt["submission_attempt_id"], request_status="RESPONDED",
        provider_response_status=str(code),
        result_state=verdict["result_state"],
        error_code=f"provider_{code}")
    await store_mod.update_publication_by_key(key, status="failed",
                                              submission_state=verdict["result_state"])
    return {"status": verdict["result_state"], "class": verdict["class"],
            "submission_attempt_id": attempt["submission_attempt_id"]}


def test_a_request_never_leaves_app(tmp_path):
    _use_tmp_db(tmp_path)
    v = classify_provider_error(None, error_kind="request_never_sent")
    assert v["class"] == NON_RETRYABLE and v["result_state"] == "FAILED"


def test_b_401_non_retryable(tmp_path):
    _use_tmp_db(tmp_path)
    out = asyncio.run(_submit(store, ScriptedPublisher([("ok", "rejected", 401)]), "K401"))
    assert out["status"] == "FAILED" and out["class"] == NON_RETRYABLE


def test_c_403_non_retryable(tmp_path):
    _use_tmp_db(tmp_path)
    v = classify_provider_error(403)
    assert v["class"] == NON_RETRYABLE and v["result_state"] == "FAILED"


def test_d_429_retryable_bounded(tmp_path):
    _use_tmp_db(tmp_path)
    v = classify_provider_error(429)
    assert v["class"] == RETRYABLE
    d1 = retry_delay_s(1, 120)
    assert d1 == 120  # Retry-After honored
    import random
    d2 = retry_delay_s(1, None, random.Random(0))
    assert 2.0 <= d2 <= 2.5  # bounded exponential + jitter
    dmax = retry_delay_s(99, None, random.Random(0))
    assert dmax <= 300.0


def test_e_500_retryable(tmp_path):
    _use_tmp_db(tmp_path)
    v = classify_provider_error(500)
    assert v["class"] == RETRYABLE
    assert MAX_ATTEMPTS == 4


def test_f_accepted_confirmed(tmp_path):
    _use_tmp_db(tmp_path)
    out = asyncio.run(_submit(store, ScriptedPublisher([("ok", "published", 200)]), "KF"))
    assert out["status"] == "published" and out["post_url"].startswith("scripted://")


def test_g_response_lost_unknown_then_reconciled(tmp_path):
    _use_tmp_db(tmp_path)
    pub = ScriptedPublisher([("raise", TimeoutError("read timed out"))])

    async def _lookup(**kw):
        return pub.lookup_submission(**kw)

    out = asyncio.run(_submit(store, pub, "KG"))
    assert out["status"] == UNKNOWN_EXTERNAL_RESULT
    # blind retry is impossible: slot already claimed
    assert asyncio.run(store.claim_publication_slot("clip1", "camp1", "tiktok", "KG")) is False
    # provider actually accepted it; reconciliation confirms from evidence
    pub._registry["KG"] = {"post_url": "scripted://tiktok/KG", "status": "published",
                           "platform": "tiktok",
                           "provider_submission_id": "sub_KG"}
    res = asyncio.run(reconcile_one(store, _lookup, "KG", actor="test"))
    assert res["outcome"] == "CONFIRMED" and res["post_url"].endswith("/KG")
    attempts = asyncio.run(store.list_submission_attempts("KG"))
    assert len(attempts) == 1  # history preserved, not rewritten


def test_h_lookup_finds_nothing_controlled_retry(tmp_path):
    _use_tmp_db(tmp_path)
    pub = ScriptedPublisher([("raise", TimeoutError("x"))])

    async def _lookup(**kw):
        return pub.lookup_submission(**kw)

    asyncio.run(_submit(store, pub, "KH"))
    res = asyncio.run(reconcile_one(store, _lookup, "KH", actor="test"))
    assert res["outcome"] == "NOT_FOUND"
    pub2 = ScriptedPublisher([("ok", "published", 200)])
    out = asyncio.run(_submit(store, pub2, "KH2"))
    assert out["status"] == "published"


def test_i_lookup_unavailable_still_unknown(tmp_path):
    _use_tmp_db(tmp_path)

    async def _nolookup(**kw):
        raise LookupUnavailable("no lookup op")

    asyncio.run(store.claim_publication_slot("clip1", "camp1", "tiktok", "KI"))
    asyncio.run(store.update_publication_by_key("KI", status="unknown",
                                                submission_state=UNKNOWN_EXTERNAL_RESULT))
    res = asyncio.run(reconcile_one(store, _nolookup, "KI", actor="test"))
    assert res["outcome"] == "STILL_UNKNOWN"
    unresolved = asyncio.run(find_unresolved(store))
    assert any(p["idempotency_key"] == "KI" for p in unresolved)


def test_j_duplicate_key_recovers_existing(tmp_path):
    _use_tmp_db(tmp_path)
    m = MockPublisher()
    r1 = m.publish("c", "t", "d", "tiktok", "KJ")
    r2 = m.publish("c", "t", "d", "tiktok", "KJ")
    assert r2.get("duplicate") is True and r2["post_url"] == r1["post_url"]
    assert m.calls == 2  # two calls, one submission


def test_k_concurrent_requests_one_claim(tmp_path):
    _use_tmp_db(tmp_path)
    results = []

    def worker():
        results.append(asyncio.run(
            store.claim_publication_slot("clip1", "camp1", "tiktok", "KK")))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and results.count(False) == 7


def test_l_scheduler_restart_preserves_unknown(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(store.init_automation_tables())
    sched = AutomationScheduler(max_retries=0)
    asyncio.run(store.upsert_job("pub1", "publish_clip", "running", "camp1", "clip1",
                                 payload={"submission_state": "UNKNOWN_EXTERNAL_RESULT"}))
    asyncio.run(store.heartbeat_job("pub1", "dead-worker", "publish"))
    actions = AutomationScheduler.recover_interrupted(
        [{"job_id": "pub1", "status": "running", "kind": "publish_clip",
          "attempt": 2, "heartbeat_at": "2020-01-01T00:00:00+00:00",
          "payload_json": "{\"submission_state\": \"UNKNOWN_EXTERNAL_RESULT\"}"}])
    assert actions[0]["action"] == "require_review"


def test_no_live_to_mock_fallback():
    with pytest.raises(PublishBlocked):
        resolve_publisher("authorized", live=False)
    assert isinstance(resolve_publisher("mock", live=True), MockPublisher)
    with pytest.raises(PublishBlocked):
        resolve_publisher("nope", live=True)


def test_capability_states_explicit(monkeypatch):
    monkeypatch.delenv("PUBLISH_TOKEN_TIKTOK", raising=False)
    monkeypatch.delenv("PUBLISH_PLATFORMS", raising=False)
    s = adapter_status(MockPublisher(), "tiktok")
    assert s["state"] == "OPERATIONAL"  # simulator, honest label
    from automation.publishing import AuthorizedPlatformPublisher
    s = adapter_status(AuthorizedPlatformPublisher(), "tiktok")
    assert s["state"] in ("SUPPORTED", "CONFIGURED", "AUTHENTICATED",
                          "AVAILABLE", "OPERATIONAL", "UNAVAILABLE")
    assert s["state"] == "SUPPORTED"  # nothing configured here
    caps = AuthorizedPlatformPublisher.capabilities()
    assert set(caps) == {"supports_submission_lookup", "supports_idempotency_keys",
                         "supports_submission_status", "supports_delete",
                         "supports_analytics", "supports_retry_after"}


def test_ambiguous_never_auto_failed(tmp_path):
    _use_tmp_db(tmp_path)
    for kind in ("timeout", "connection_error", "timeout_after_transmission",
                 "response_lost"):
        v = classify_provider_error(None, error_kind=kind)
        assert v["class"] == AMBIGUOUS, kind
        assert v["result_state"] == UNKNOWN_EXTERNAL_RESULT, kind


def test_canonical_key_normalization():
    a = canonical_idem_key(" Camp ", "clip1", "rh1", "TikTok")
    b = canonical_idem_key("camp", "clip1", "rh1", "tiktok")
    assert a == b
