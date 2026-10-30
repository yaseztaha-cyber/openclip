"""
Tests for analysis cache module (Phase 5.3).
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import analysis_cache as ac


def test_cache_key_deterministic():
    k1 = ac._cache_key("a", "b", "c")
    k2 = ac._cache_key("a", "b", "c")
    assert k1 == k2


def test_cache_key_different_inputs():
    k1 = ac._cache_key("a", "b")
    k2 = ac._cache_key("a", "c")
    assert k1 != k2


def test_set_and_get_cached():
    with tempfile.TemporaryDirectory() as tmpdir:
        ac.CACHE_BASE = os.path.join(tmpdir, "cache")
        key = ac._cache_key("test", "1")
        ac.set_cached("test", key, {"value": 42})
        result = ac.get_cached("test", key)
        assert result == {"value": 42}


def test_cache_miss():
    with tempfile.TemporaryDirectory() as tmpdir:
        ac.CACHE_BASE = os.path.join(tmpdir, "cache")
        result = ac.get_cached("test", "nonexistent")
        assert result is None


def test_cache_invalidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        ac.CACHE_BASE = os.path.join(tmpdir, "cache")
        key = ac._cache_key("test", "1")
        ac.set_cached("test", key, {"value": 42})
        assert ac.invalidate_cache("test", key) is True
        assert ac.get_cached("test", key) is None


def test_cache_clear():
    with tempfile.TemporaryDirectory() as tmpdir:
        ac.CACHE_BASE = os.path.join(tmpdir, "cache")
        key = ac._cache_key("test", "1")
        ac.set_cached("test", key, {"value": 42})
        count = ac.clear_cache("test")
        assert count == 1
        assert ac.get_cached("test", key) is None


def test_cache_stats():
    with tempfile.TemporaryDirectory() as tmpdir:
        ac.CACHE_BASE = os.path.join(tmpdir, "cache")
        key = ac._cache_key("test", "1")
        ac.set_cached("test", key, {"value": 42})
        stats = ac.cache_stats()
        assert stats["total_entries"] == 1
        assert stats["by_type"]["test"] == 1


def test_cache_version_invalidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        ac.CACHE_BASE = os.path.join(tmpdir, "cache")
        key = ac._cache_key("test", "1")
        ac.set_cached("test", key, {"value": 42})
        # Change version
        old_version = ac.CACHE_VERSION
        ac.CACHE_VERSION = "different-version"
        result = ac.get_cached("test", key)
        assert result is None
        ac.CACHE_VERSION = old_version


def test_cached_pipeline():
    with tempfile.TemporaryDirectory() as tmpdir:
        ac.CACHE_BASE = os.path.join(tmpdir, "cache")
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        result = ac.run_cached_pipeline(vid, tmpdir, use_cache=True)
        assert result["candidate_count"] >= 0
        assert "timing" in result
        assert "cache_misses" in result
        # Second run should have cache hits
        result2 = ac.run_cached_pipeline(vid, tmpdir, use_cache=True)
        assert len(result2["cache_hits"]) > 0


def test_no_network():
    import socket
    original = socket.socket.connect
    def blocked(self, *args, **kwargs):
        raise RuntimeError("NETWORK")
    socket.socket.connect = blocked
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ac.CACHE_BASE = os.path.join(tmpdir, "cache")
            key = ac._cache_key("test", "1")
            ac.set_cached("test", key, {"value": 42})
            assert ac.get_cached("test", key) == {"value": 42}
    finally:
        socket.socket.connect = original
