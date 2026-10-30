"""
Tests for Phase 5 benchmark and ablation (Phase 4.1.4+5).
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import phase5_benchmark as p5
import benchmark_dataset as bd


def test_import_authorized_video():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        # Create a tiny test file
        test_file = os.path.join(tmpdir, "test.mp4")
        with open(test_file, "wb") as f:
            f.write(b"fake video data")
        meta = p5.import_authorized_video(tmpdir, test_file, "v1", permission_status="authorized")
        assert meta.video_id == "v1"
        assert meta.permission_status == "authorized"
        assert meta.source_hash != ""


def test_import_denied_permission():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        test_file = os.path.join(tmpdir, "test.mp4")
        with open(test_file, "wb") as f:
            f.write(b"fake video data")
        try:
            p5.import_authorized_video(tmpdir, test_file, "v1", permission_status="denied")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


def test_import_nonexistent_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        try:
            p5.import_authorized_video(tmpdir, "/nonexistent/file.mp4", "v1")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass


def test_assign_partition_deterministic():
    p1 = p5.assign_partition("video_1", 10)
    p2 = p5.assign_partition("video_1", 10)
    assert p1 == p2


def test_assign_partition_valid():
    p = p5.assign_partition("video_1", 10)
    assert p in ("dev", "val", "test")


def test_partition_no_leakage():
    """Same video always gets same partition."""
    partitions = [p5.assign_partition("video_x", 10) for _ in range(100)]
    assert len(set(partitions)) == 1


def test_create_partition_manifest():
    with tempfile.TemporaryDirectory() as tmpdir:
        bd.init_dataset(tmpdir)
        bd.add_video(tmpdir, bd.VideoMetadata(video_id="v1", permission_status="authorized"))
        bd.add_video(tmpdir, bd.VideoMetadata(video_id="v2", permission_status="authorized"))
        manifest = p5.create_partition_manifest(tmpdir)
        assert "dev" in manifest
        assert "val" in manifest
        assert "test" in manifest
        total = len(manifest["dev"]) + len(manifest["val"]) + len(manifest["test"])
        assert total == 2


def test_run_ablation_study():
    with tempfile.TemporaryDirectory() as tmpdir:
        import evaluation_engine as ee
        vid = os.path.join(tmpdir, "test.mp4")
        ee.create_synthetic_video(vid, 10.0, "minimal")
        result = p5.run_ablation_study(vid, tmpdir)
        assert "transcript_only" in result
        assert "audio_only" in result
        assert "visual_only" in result
        assert "full_multimodal" in result
        for name, data in result.items():
            assert "candidate_count" in data


def test_no_network():
    import socket
    original = socket.socket.connect
    def blocked(self, *args, **kwargs):
        raise RuntimeError("NETWORK")
    socket.socket.connect = blocked
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.mp4")
            with open(test_file, "wb") as f:
                f.write(b"fake")
            bd.init_dataset(tmpdir)
            p5.import_authorized_video(tmpdir, test_file, "v1", permission_status="authorized")
            p5.assign_partition("v1", 5)
    finally:
        socket.socket.connect = original
