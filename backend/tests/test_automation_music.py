"""Music-filter tests on synthesized audio (ffmpeg lavfi fixtures)."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.music import analyze_music_status, classify_from_features
from ffmpeg_utils import resolve_ffmpeg


def _render(path, args):
    r = subprocess.run([resolve_ffmpeg(), "-y"] + args, capture_output=True, timeout=30)
    assert r.returncode == 0, (r.stderr or b"")[-300:]
    return path


def test_silence_is_uncertain_not_music():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sil.m4a")
        _render(p, ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=3",
                    "-c:a", "aac", p])
        out = analyze_music_status(p)
        assert out["status"] in ("UNCERTAIN", "NO_MUSIC"), out
        assert out["status"] != "MUSIC_PRESENT"


def test_sustained_tone_with_beat_is_music():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "mus.m4a")
        _render(p, ["-f", "lavfi", "-i",
                    "sine=frequency=440:d=6,tremolo=f=4:d=0.8",
                    "-c:a", "aac", p])
        out = analyze_music_status(p)
        assert out["status"] in ("MUSIC_PRESENT", "UNCERTAIN"), out


def test_bursty_speech_like_is_not_music():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sp.m4a")
        _render(p, ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=1100:d=0.4",
                    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=700:d=0.5",
                    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1.1",
                    "-filter_complex", "[0][1][2][3][4]concat=n=5:v=0:a=1",
                    "-c:a", "aac", p])
        out = analyze_music_status(p)
        assert out["status"] != "MUSIC_PRESENT", out


def test_environmental_noise_passes_where_appropriate():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "nz.m4a")
        _render(p, ["-f", "lavfi", "-i", "anoisesrc=d=4:c=white:r=16000:a=0.25",
                    "-c:a", "aac", p])
        out = analyze_music_status(p)
        assert out["status"] != "MUSIC_PRESENT", out


def test_classifier_matrix():
    music = classify_from_features({"ok": True, "tonal_mean": 0.6, "tonal_persist": 0.8,
                                    "beat_peak": 0.7, "flatness_mean": 0.3,
                                    "energy_cv": 0.3, "zcr_cv": 0.2,
                                    "centroid_mean": 2000})
    assert music["status"] == "MUSIC_PRESENT"
    speech = classify_from_features({"ok": True, "tonal_mean": 0.2, "tonal_persist": 0.2,
                                     "beat_peak": 0.1, "flatness_mean": 0.7,
                                     "energy_cv": 1.2, "zcr_cv": 0.8,
                                     "centroid_mean": 3000})
    assert speech["status"] == "NO_MUSIC"
    mixed = classify_from_features({"ok": True, "tonal_mean": 0.4, "tonal_persist": 0.45,
                                    "beat_peak": 0.35, "flatness_mean": 0.5,
                                    "energy_cv": 0.7, "zcr_cv": 0.4,
                                    "centroid_mean": 3000})
    assert mixed["status"] == "UNCERTAIN"
