"""
Regression tests for fast single-pass audio signals (P0).
Covers: silent, constant, loud transient, normal speech, long video, missing audio, corrupted.
"""
import os
import subprocess
import tempfile
import time

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import signals
from ffmpeg_utils import resolve_ffmpeg


def _make_silent_video(path: str, duration: float = 2.0):
    cmd = [resolve_ffmpeg(), "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
           "-t", str(duration), "-c:a", "aac", path]
    r = subprocess.run(cmd, capture_output=True, timeout=10)
    assert r.returncode == 0, r.stderr.decode()[:300]
    assert os.path.exists(path)


def _make_tone_video(path: str, duration: float = 3.0, freq: int = 440, volume: float = 0.5):
    # Constant tone via sine
    cmd = [resolve_ffmpeg(), "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}:sample_rate=16000",
           "-af", f"volume={volume}", "-c:a", "aac", path]
    r = subprocess.run(cmd, capture_output=True, timeout=10)
    assert r.returncode == 0


def _make_burst_video(path: str):
    # 3s: 1s silence + 0.3s loud burst + 1.7s normal
    # Use amerge of silence and tone
    cmd = [
        resolve_ffmpeg(), "-y",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1",
        "-f", "lavfi", "-i", "sine=frequency=1000:d=0.3",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1.7",
        "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1",
        "-c:a", "aac", path,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=10)
    # Fallback if concat fails: just create normal video
    if r.returncode != 0:
        _make_tone_video(path, duration=3.0)


def test_silent_audio():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "silent.m4a")
        _make_silent_video(p, 2.0)
        r = signals.extract_audio_signals(p, 2.0)
        assert r["_debug"]["source"] in ("waveform_silent", "waveform", "fallback_fake")
        # For true silence, loudness should be 0 and silence 1
        if r["_debug"]["source"] == "waveform_silent":
            assert r["loudness"] == [0.0, 0.0]
            assert r["silence"] == [1.0, 1.0]
            assert r["burst"] == [0.0, 0.0]
        else:
            # Fallback case: at least silence should be high
            assert sum(r["silence"]) >= 1.0


def test_constant_audio():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "const.m4a")
        _make_tone_video(p, 3.0, volume=0.3)
        r = signals.extract_audio_signals(p, 3.0)
        # Constant tone → loudness relatively flat, burst low, silence mostly 0
        assert len(r["loudness"]) == 3
        # Burst should be low for constant (no jumps)
        assert max(r["burst"]) < 0.8, f"constant should not burst {r['burst']}"
        # Silence should be mostly 0 (allow 1 silent second due to windowing)
        assert sum(r["silence"]) <= 1.5


def test_loud_transient():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "burst.m4a")
        _make_burst_video(p)
        if not os.path.exists(p):
            return
        r = signals.extract_audio_signals(p, 3.0)
        # Burst should have a peak in middle
        assert max(r["burst"]) > 0.5, f"Expected burst peak, got {r['burst']}"
        # Loudness should not be flat
        assert len(set(round(v, 2) for v in r["loudness"])) > 1


def test_normal_speech_real_video():
    # Use existing real video if available — pick source (not clip, not f299)
    import glob, re
    candidates = glob.glob(r"D:\YASEZ_HOME\autoclip\Openclip-main\Openclip-main\tmp\*\*.mp4")
    # Filter: clips subdir excluded, f299/f140 video-only excluded, temp excluded
    def is_source(p: str) -> bool:
        base = os.path.basename(p)
        if "clips" in p: return False
        if re.search(r"\.f\d+\.mp4$", base): return False
        if ".temp." in base: return False
        return os.path.getsize(p) > 5 * 1024 * 1024
    src_candidates = [c for c in candidates if is_source(c)]
    if not src_candidates:
        src_candidates = [c for c in candidates if os.path.getsize(c) > 5 * 1024 * 1024 and "clips" not in c]
    if not src_candidates:
        return
    video = max(src_candidates, key=lambda p: os.path.getsize(p))
    r = signals.extract_audio_signals(video, 10)
    assert r["_debug"]["source"] in ("waveform", "waveform_silent", "fallback_volumedetect"), f"got {r['_debug']}"
    assert len(r["loudness"]) == 10
    # Normal speech should be variable, not flat 0.5 (allow fallback but check not all same)
    if r["_debug"]["source"] == "waveform":
        assert len(set(round(v, 2) for v in r["loudness"])) > 2
    assert 0 <= max(r["burst"]) <= 1.0


def test_long_video_performance():
    import glob, re
    candidates = glob.glob(r"D:\YASEZ_HOME\autoclip\Openclip-main\Openclip-main\tmp\*\*.mp4")
    def is_source(p: str) -> bool:
        base = os.path.basename(p)
        if "clips" in p: return False
        if re.search(r"\.f\d+\.mp4$", base): return False
        if ".temp." in base: return False
        return os.path.getsize(p) > 5 * 1024 * 1024
    src_candidates = [c for c in candidates if is_source(c)]
    if not src_candidates:
        src_candidates = [c for c in candidates if os.path.getsize(c) > 5 * 1024 * 1024 and "clips" not in c]
    if not src_candidates:
        return
    video = max(src_candidates, key=lambda p: os.path.getsize(p))
    t0 = time.time()
    r = signals.extract_audio_signals(video, 60)
    elapsed = time.time() - t0
    assert elapsed < 10.0, f"60s should be <10s, took {elapsed:.1f}s"
    assert len(r["loudness"]) == 60


def test_missing_audio():
    r = signals.extract_audio_signals(r"C:\nonexistent_xyz.mp4", 5)
    assert r["_debug"]["source"] in ("fallback_fake", "fallback_volumedetect")
    assert len(r["loudness"]) == 5


def test_corrupted_input():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "corrupt.mp4")
        with open(p, "wb") as f:
            f.write(b"not a video")
        r = signals.extract_audio_signals(p, 5)
        # Should fallback gracefully, not crash
        assert len(r["loudness"]) == 5
        assert r["_debug"]["source"] in ("fallback_fake", "fallback_volumedetect")


def test_audio_extraction_failure_fallback():
    # Ensure fallback is explicit, not silent fake
    r = signals.extract_audio_signals(r"C:\nonexistent.mp4", 5)
    assert r.get("_debug", {}).get("source") is not None
    assert r["_debug"]["source"].startswith("fallback")
