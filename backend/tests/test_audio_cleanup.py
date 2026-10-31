"""
Tests for audio cleanup module (Phase 5.3.2).
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audio_cleanup as ac


def test_inspect_video_no_audio():
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        insp = ac.inspect_video(vid)
        assert insp.duration > 0
        assert insp.error == ""


def test_inspect_nonexistent():
    insp = ac.inspect_video("/nonexistent/video.mp4")
    assert insp.error != ""


def test_process_mute_all():
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        out = os.path.join(tmpdir, "muted.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        result = ac.process_mute_all(vid, out)
        assert result.status == "verified_silent"
        assert os.path.exists(out)
        assert result.processing_time >= 0


def test_process_mute_all_preserves_video():
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        out = os.path.join(tmpdir, "muted.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        result = ac.process_mute_all(vid, out)
        assert result.video_preserved is True


def test_process_review():
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        result = ac.process_review(vid)
        assert result.status == "needs_review"
        assert result.mode == "review"


def test_get_separation_model_status():
    status = ac.get_separation_model_status()
    assert "available" in status
    assert "name" in status
    assert "message" in status
    assert status["available"] is False  # No demucs installed


def test_process_remove_music():
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        out = os.path.join(tmpdir, "cleaned.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        result = ac.process_remove_music(vid, out)
        # Without source separation model, should be needs_review (FFmpeg filtering)
        assert result.status in ("needs_review", "failed")
        assert result.model_available is False


def test_inspection_to_dict():
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        insp = ac.inspect_video(vid)
        d = ac.inspection_to_dict(insp)
        assert "duration" in d
        assert "has_audio" in d


def test_no_network():
    import socket
    original = socket.socket.connect
    def blocked(self, *args, **kwargs):
        raise RuntimeError("NETWORK")
    socket.socket.connect = blocked
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            import evaluation_engine as ee
            vid = os.path.join(tmpdir, "test.mp4")
            ee.create_synthetic_video(vid, 10.0, "minimal")
            result = ac.process_mute_all(vid, os.path.join(tmpdir, "out.mp4"))
            assert result.status == "verified_silent"
    finally:
        socket.socket.connect = original
