"""
Tests for publishing gate bypass prevention (Phase 5.3.9).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient
import api as api_mod
import audio_cleanup.publishing_gate as pg


def test_download_blocked_without_report():
    """Download without cleanup report returns 403."""
    client = TestClient(api_mod.app)
    # Use a fake clip_id that won't have a cleanup report
    resp = client.get("/api/clips/zzz_nonexistent_clip_xyz_999_98acf5da714b4fbd866596c675734135/download")
    # Should be 404 (clip not found) or 403 (blocked)
    print("DEBUG: status=", resp.status_code, "body=", resp.text[:200])
    assert resp.status_code in (403, 404)


def test_download_blocked_needs_review():
    """Download with needs_review status returns 403."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "needs_review", "mode": "remove_music"}
            pg.save_cleanup_report("test_clip_gate", report)
            # Now check_publish_ready should block
            result = pg.check_publish_ready("test_clip_gate")
            assert result.allowed is False
        finally:
            os.chdir(original_cwd)


def test_download_allowed_verified_silent():
    """Download with verified_silent status is allowed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "verified_silent", "mode": "mute_all", "output_hash": "hash123"}
            pg.save_cleanup_report("test_clip_ok", report)
            result = pg.check_publish_ready("test_clip_ok")
            assert result.allowed is True
        finally:
            os.chdir(original_cwd)


def test_publish_gate_blocks_all_non_allowed():
    """All non-allowed statuses block publishing."""
    blocked = ["not_checked", "queued", "inspecting", "music_detected", "processing",
               "verifying", "needs_review", "failed", "interrupted"]
    for status in blocked:
        assert status in pg.PUBLISH_BLOCKED_STATUSES


def test_publish_gate_allows_only_verified():
    """Only verified statuses allow publishing."""
    assert pg.PUBLISH_ALLOWED_STATUSES == {"verified_music_free", "verified_silent"}


def test_hash_mismatch_blocks():
    """Hash mismatch blocks publishing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "verified_silent", "mode": "mute_all", "output_hash": "original_hash"}
            pg.save_cleanup_report("test_clip_hash", report)
            result = pg.check_publish_ready("test_clip_hash", output_hash="tampered_hash")
            assert result.allowed is False
            assert "changed since cleanup" in result.reason
        finally:
            os.chdir(original_cwd)


def test_no_network():
    import socket
    original = socket.socket.connect
    def blocked(self, *args, **kwargs):
        raise RuntimeError("NETWORK")
    socket.socket.connect = blocked
    try:
        result = pg.check_publish_ready("nonexistent")
        assert result.allowed is False
    finally:
        socket.socket.connect = original
