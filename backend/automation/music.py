"""
music.py — Real background-music detection from audio signals.

Uses the project's existing ffmpeg-based audio path (same approach as
signals.py: single-pass PCM) plus numpy spectral/temporal features:

  - spectral flatness / centroid (tonal music vs noisy speech)
  - tonal persistence across windows (sustained musical notes)
  - energy autocorrelation peak (repeating beat/loop structure)
  - zero-crossing-rate variance (speech is bursty, music is steady)

Outputs NO_MUSIC | MUSIC_PRESENT | UNCERTAIN. When the evidence is weak
or the audio is near-silent, the honest answer is UNCERTAIN — never a
pretended certainty. Environmental noise alone must not reject speech.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ffmpeg_utils import resolve_ffmpeg
except Exception:  # pragma: no cover
    def resolve_ffmpeg() -> str:
        return "ffmpeg"

NO_MUSIC = "NO_MUSIC"
MUSIC_PRESENT = "MUSIC_PRESENT"
UNCERTAIN = "UNCERTAIN"

_SR = 16000  # analysis sample rate


def _read_pcm(video_path: str, start: float = 0.0, duration: float | None = None) -> bytes:
    cmd = [resolve_ffmpeg(), "-v", "error", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", video_path, "-vn", "-ac", "1", "-ar", str(_SR), "-f", "s16le", "pipe:1"]
    if duration:
        cmd += ["-t", f"{duration:.2f}"]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    return proc.stdout or b""


def _features(pcm: bytes) -> dict:
    import numpy as np  # type: ignore

    if len(pcm) < _SR * 2:  # need >= 1s of audio
        return {"ok": False, "reason": "too_little_audio"}
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0
    # 1s non-overlapping windows
    n_win = len(samples) // _SR
    samples = samples[: n_win * _SR].reshape(n_win, _SR)

    rms = np.sqrt(np.mean(samples ** 2, axis=1) + 1e-12)
    # zero-crossing rate per window
    zc = np.mean(np.abs(np.diff(np.sign(samples), axis=1)) > 0, axis=1)

    centroids: list[float] = []
    flatness: list[float] = []
    tonal: list[float] = []
    for w in samples:
        spec = np.abs(np.fft.rfft(w * np.hanning(len(w)))) + 1e-12
        freqs = np.fft.rfftfreq(len(w), 1.0 / _SR)
        centroids.append(float(np.sum(freqs * spec) / np.sum(spec)))
        flatness.append(float(math.exp(np.mean(np.log(spec))) / (np.mean(spec) + 1e-12)))
        # tonal peakiness: share of energy in top 5% bins
        k = max(1, len(spec) // 20)
        top = np.sort(spec)[-k:]
        tonal.append(float(np.sum(top) / (np.sum(spec) + 1e-12)))

    rms = np.asarray(rms)
    energy_mean = float(np.mean(rms))
    if energy_mean < 0.005:
        return {"ok": False, "reason": "near_silence", "energy_mean": energy_mean}

    # Beat regularity: autocorrelation of the RMS envelope
    env = (rms - rms.mean()) / (rms.std() + 1e-9)
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    ac_peak = float(np.max(ac[1:]) / (ac[0] + 1e-9)) if len(ac) > 1 else 0.0

    return {
        "ok": True,
        "energy_mean": energy_mean,
        "energy_cv": float(rms.std() / (rms.mean() + 1e-9)),
        "zcr_mean": float(np.mean(zc)),
        "zcr_cv": float(np.std(zc) / (np.mean(zc) + 1e-9)),
        "centroid_mean": float(np.mean(centroids)),
        "flatness_mean": float(np.mean(flatness)),
        "tonal_mean": float(np.mean(tonal)),
        "tonal_persist": float(1.0 - np.std(tonal) / (np.mean(tonal) + 1e-9)),
        "beat_peak": max(0.0, ac_peak),
        "windows": n_win,
    }


def classify_from_features(f: dict) -> dict:
    """Map feature dict → {status, confidence, metrics}."""
    if not f.get("ok"):
        return {"status": UNCERTAIN, "confidence": 0.3,
                "reason": f.get("reason", "analysis_failed"), "metrics": f}
    tonal = f["tonal_mean"]
    persist = f["tonal_persist"]
    beat = f["beat_peak"]
    flat = f["flatness_mean"]
    zcr_cv = f["zcr_cv"]
    energy_cv = f["energy_cv"]

    # Music-dominant: sustained tonal content + steady energy + repeating structure
    music_votes = 0
    if tonal > 0.45 and persist > 0.55:
        music_votes += 2
    elif tonal > 0.35:
        music_votes += 1
    if beat > 0.45:
        music_votes += 2
    elif beat > 0.3:
        music_votes += 1
    if energy_cv < 0.6 and flat < 0.45:
        music_votes += 1  # steady, non-noisy bed
    if f["centroid_mean"] < 6000 and tonal > 0.4:
        music_votes += 1

    # Speech-like: bursty energy + highly variable ZCR + non-persistent tone
    speech_votes = 0
    if energy_cv > 0.9:
        speech_votes += 1
    if zcr_cv > 0.5:
        speech_votes += 1
    if persist < 0.35:
        speech_votes += 1
    if flat > 0.55:
        speech_votes += 1  # noisy / environmental

    metrics = {k: round(v, 4) if isinstance(v, float) else v for k, v in f.items()}
    if music_votes >= 4 and music_votes - speech_votes >= 2:
        return {"status": MUSIC_PRESENT, "confidence": min(0.95, 0.55 + 0.1 * music_votes),
                "reason": "tonal_persistent_repeating_audio", "metrics": metrics}
    if speech_votes >= 3 and speech_votes > music_votes:
        return {"status": NO_MUSIC, "confidence": min(0.9, 0.5 + 0.12 * speech_votes),
                "reason": "speech_like_or_environmental", "metrics": metrics}
    return {"status": UNCERTAIN, "confidence": 0.4,
            "reason": "mixed_or_weak_evidence", "metrics": metrics}


def analyze_music_status(video_path: str, start: float = 0.0,
                         duration: float | None = None) -> dict:
    """Full pipeline: extract PCM → features → classification."""
    try:
        pcm = _read_pcm(video_path, start=start, duration=duration)
    except Exception as exc:
        logger.warning("music PCM extraction failed: %s", exc)
        return {"status": UNCERTAIN, "confidence": 0.3, "reason": "extraction_failed", "metrics": {}}
    try:
        feats = _features(pcm)
    except Exception as exc:
        logger.warning("music feature extraction failed: %s", exc)
        return {"status": UNCERTAIN, "confidence": 0.3, "reason": "feature_failed", "metrics": {}}
    return classify_from_features(feats)
