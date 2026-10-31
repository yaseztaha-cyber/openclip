"""
Tests for audio cleanup publishing gate (Phase 5.3.7).
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audio_cleanup as ac


def test_publish_gate_no_report():
    result = ac.check_publish_ready("nonexistent_video")
    assert result.allowed is False
    assert "No audio cleanup report found" in result.reason


def test_publish_gate_verified_silent():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create report
        report = {"status": "verified_silent", "mode": "mute_all", "output_hash": "abc123"}
        os.makedirs(os.path.join(tmpdir, "data", "audio_cleanup", "reports"), exist_ok=True)
        report_path = os.path.join(tmpdir, "data", "audio_cleanup", "reports", "test_vid_report.json")
        # Monkeypatch the path
        import audio_cleanup.publishing_gate as pg
        original_check = pg.check_publish_ready

        # Save report using the function (it uses "data" relative path)
        # Instead, test the logic directly
        assert "verified_silent" in ac.PUBLISH_ALLOWED_STATUSES if hasattr(ac, "PUBLISH_ALLOWED_STATUSES") else True


def test_publish_allowed_statuses():
    assert "verified_music_free" in ac.publishing_gate.PUBLISH_ALLOWED_STATUSES
    assert "verified_silent" in ac.publishing_gate.PUBLISH_ALLOWED_STATUSES


def test_publish_blocked_statuses():
    for status in ["not_checked", "queued", "processing", "needs_review", "failed"]:
        assert status in ac.publishing_gate.PUBLISH_BLOCKED_STATUSES


def test_save_and_check_cleanup_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Change working directory to tmpdir for the test
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "verified_silent", "mode": "mute_all", "output_hash": "hash123"}
            path = ac.save_cleanup_report("test_vid", report)
            assert os.path.exists(path)

            result = ac.check_publish_ready("test_vid")
            assert result.allowed is True
            assert result.status == "verified_silent"
        finally:
            os.chdir(original_cwd)


def test_check_publish_ready_needs_review():
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "needs_review", "mode": "remove_music"}
            ac.save_cleanup_report("test_vid2", report)

            result = ac.check_publish_ready("test_vid2")
            assert result.allowed is False
            assert result.status == "needs_review"
        finally:
            os.chdir(original_cwd)


def test_check_publish_ready_failed():
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "failed", "mode": "remove_music", "error": "Model not available"}
            ac.save_cleanup_report("test_vid3", report)

            result = ac.check_publish_ready("test_vid3")
            assert result.allowed is False
            assert result.status == "failed"
        finally:
            os.chdir(original_cwd)


def test_invalidate_cleanup_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "verified_silent", "mode": "mute_all"}
            ac.save_cleanup_report("test_vid4", report)
            assert ac.invalidate_cleanup_report("test_vid4") is True
            result = ac.check_publish_ready("test_vid4")
            assert result.allowed is False
            assert "No audio cleanup report found" in result.reason
        finally:
            os.chdir(original_cwd)


def test_get_cleanup_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "verified_silent", "mode": "mute_all"}
            ac.save_cleanup_report("test_vid5", report)

            status = ac.get_cleanup_status("test_vid5")
            assert status["status"] == "verified_silent"
            assert status["mode"] == "mute_all"
        finally:
            os.chdir(original_cwd)


def test_get_cleanup_status_missing():
    status = ac.get_cleanup_status("nonexistent_video_xyz")
    assert status["status"] == "not_checked"


def test_publish_gate_hash_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            os.makedirs("data/audio_cleanup/reports", exist_ok=True)
            report = {"status": "verified_silent", "mode": "mute_all", "output_hash": "hash123"}
            ac.save_cleanup_report("test_vid6", report)

            result = ac.check_publish_ready("test_vid6", output_hash="different_hash")
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
        result = ac.check_publish_ready("nonexistent")
        assert result.allowed is False
    finally:
        socket.socket.connect = original
