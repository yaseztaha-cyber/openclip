"""
P1 regression: tutorial input must still output 1080x1920 vertical full-bleed.
"""
import asyncio
import os
import subprocess
import tempfile

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import reframer
from ffmpeg_utils import resolve_ffmpeg, resolve_ffprobe
import json


async def _make_test_video(path: str, duration: float = 2.0):
    # Use existing real video as source for realistic face/content test
    import glob
    real = glob.glob(r"D:\YASEZ_HOME\autoclip\Openclip-main\Openclip-main\tmp\*\*.mp4")
    real = [p for p in real if os.path.getsize(p) > 5*1024*1024]
    if real:
        import shutil
        shutil.copy(max(real, key=lambda p: os.path.getsize(p)), path)
        return
    # Fallback synthetic
    cmd = [
        resolve_ffmpeg(), "-y",
        "-f", "lavfi", "-i", f"color=c=white:s=1920x1080:d={duration}:r=30",
        "-f", "lavfi", "-i", f"color=c=blue:s=200x200:d={duration}:r=30",
        "-filter_complex", "[0][1]overlay=100:100",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(duration), path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"test video create failed: {result.stderr[:500]}"


def test_tutorial_still_vertical():
    async def _run():
        with tempfile.TemporaryDirectory(prefix="openclip_test_") as tmpdir:
            src = os.path.join(tmpdir, "src.mp4")
            await _make_test_video(src, duration=2.0)
            # Use a 10s window where we know face exists (from earlier probe, 0-10s has face)
            # Copy 10s slice for test
            tmp_src = os.path.join(tmpdir, "slice.mp4")
            cmd = [resolve_ffmpeg(), "-y", "-ss", "0", "-i", src, "-t", "5", "-c", "copy", tmp_src]
            subprocess.run(cmd, capture_output=True, timeout=15)
            src_use = tmp_src if os.path.exists(tmp_src) and os.path.getsize(tmp_src) > 1024 else src
            # Force tutorial layout via options, ensure aspect forced to 9:16 by pipeline
            suggestion = {"start": 0.0, "end": 5.0, "title": "test", "reason": "test", "viral_score": 5}
            out = await reframer.process_clip(src_use, suggestion, tmpdir, "test-vertical",
                                              options={"aspect": "9:16", "layout_mode": "tutorial"})
            assert os.path.exists(out["file_path"]), "output missing"
            assert os.path.getsize(out["file_path"]) > 50 * 1024, "output too small"
            # Probe dimensions
            result = subprocess.run(
                [resolve_ffprobe(), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", out["file_path"]],
                capture_output=True, text=True, timeout=15,
            )
            assert result.returncode == 0, f"ffprobe failed: {result.stderr}"
            data = json.loads(result.stdout)
            w, h = data["streams"][0]["width"], data["streams"][0]["height"]
            assert w == 1080 and h == 1920, f"tutorial must be 1080x1920, got {w}x{h}"
            # Also check DB aspect stored is 9:16
            assert out["aspect"] == "9:16", f"aspect should be 9:16, got {out['aspect']}"

    asyncio.run(_run())


def test_output_integrity_rejects_corrupt():
    # P8: verify integrity helper catches 0-byte
    async def _run():
        import api
        ok, reasons = await api._verify_clip_integrity("/nonexistent/path.mp4", expected_aspect="9:16", expected_duration=10.0)
        assert not ok
        assert any("missing" in r.lower() for r in reasons)
        # Create a 0-byte file and verify it fails
        fd, tmppath = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        with open(tmppath, "wb") as f:
            f.write(b"")
        ok2, reasons2 = await api._verify_clip_integrity(tmppath, expected_aspect="9:16", expected_duration=2.0)
        assert not ok2
        assert any("small" in r.lower() or "suspicious" in r.lower() for r in reasons2)
        try:
            os.unlink(tmppath)
        except: pass
    asyncio.run(_run())
