"""
signals.py — Multimodal signals for clip scoring (clips-studio inspired).

Extracts per-second audio/visual signals from the source video + transcript,
normalized PER-VIDEO so a quiet podcast and a loud stream both produce
meaningful peaks.

Signals (all 0.0-1.0 per second, aligned to video timeline):
  audio_loudness     — RMS energy curve (waveform)
  audio_burst        — sudden energy jump (silence→explosion, laughter shape)
  audio_silence      — 1.0 where silencedetect says silence (for pacing penalty)
  visual_motion      — frame-to-frame motion (select scene score via mpdecimate-like diff)
  visual_scene_cut   — 1.0 at scene boundaries
  face_presence      — 1.0 where face detected
  face_size          — normalized face height fraction
  reaction           — fused audio burst + face-size jump + motion spike

All functions are CPU-only, ffmpeg-based, and tolerate missing streams.
No heavy ML dependencies beyond existing MediaPipe (reused via reframer data).

Design notes vs clips-studio:
  - clips-studio uses YOLOv8 + TalkNet + loudness + burst + chat replay.
    Here we reuse BlazeFace face_data (already extracted), ffmpeg for
    single-pass PCM audio, and lightweight motion.
  - Normalization is per-video z-score → sigmoid, so quiet videos aren't flat.
"""

import logging
import math
import os
import re
import struct
import subprocess
import time
from typing import Optional

try:
    from ffmpeg_utils import resolve_ffmpeg, resolve_ffprobe
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ffmpeg_utils import resolve_ffmpeg, resolve_ffprobe

logger = logging.getLogger(__name__)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _normalize_per_video(values: list[float]) -> list[float]:
    """
    Z-score → sigmoid normalization so sparse loud moments in quiet videos
    still produce peaks, and screaming streams don't saturate to 1 everywhere.
    """
    if not values:
        return []
    n = len(values)
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / max(n, 1)
    std = math.sqrt(var) if var > 1e-9 else 1.0
    out: list[float] = []
    for x in values:
        z = (x - mean) / (std + 1e-9)
        s = 1.0 / (1.0 + math.exp(-z * 1.2))
        out.append(_clamp01(s))
    return out


def _percentile(values: list[float], p: float) -> float:
    """p in 0-100, e.g. 75 -> 75th percentile"""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    d0 = s[f] * (c - k)
    d1 = s[c] * (k - f)
    return d0 + d1


# ---------------------------------------------------------------------------
# Audio — single-pass PCM waveform analysis
# ---------------------------------------------------------------------------

def extract_audio_signals(video_path: str, duration: float) -> dict[str, list[float]]:
    """
    Single-pass audio extraction → per-second waveform features.

    Pipeline (one ffmpeg invocation):
      VIDEO → ffmpeg -vn -ac 1 -ar 16000 -f s16le pipe:1 → Python windowed analysis

    Per-window (1s) features:
      rms, peak, energy_delta, silence_ratio, burst

    Returns {loudness, burst, silence} as per-second 0-1 lists length ceil(duration).
    loudness: RMS normalized per-video.
    burst: positive energy delta (sudden jump).
    silence: 1.0 where silence_ratio high.

    Logs extraction/analysis time and source.
    Falls back to flat 0.5 with explicit log if no audio or ffmpeg fails.
    """
    n = max(1, math.ceil(duration))
    t0 = time.time()

    # Try single-pass PCM
    try:
        # Use -t to limit to duration (speed for long videos)
        # -vn drops video, -ac 1 mono, -ar 16000 low rate, s16le PCM
        cmd = [
            resolve_ffmpeg(),
            "-t", str(duration),
            "-i", video_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "pipe:1",
        ]
        # Use Popen to stream; timeout covers whole extraction
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            pcm_data, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            pcm_data, stderr = proc.communicate()
            raise RuntimeError(f"ffmpeg PCM extraction timed out after 30s for {duration:.0f}s video")

        if proc.returncode != 0:
            # Check if error is "Output file #0 does not contain any stream" → no audio
            err = stderr.decode(errors="ignore") if isinstance(stderr, bytes) else str(stderr)
            if "does not contain any stream" in err or "Stream #0:0" not in err and "Audio:" not in err:
                # Try to detect if file has audio via ffprobe
                if not _has_audio_stream(video_path):
                    logger.info("Audio signals: no audio stream in %s — using silence fallback", os.path.basename(video_path))
                    raise RuntimeError("no audio stream")
            # Other ffmpeg error
            if proc.returncode != 0 and not pcm_data:
                raise RuntimeError(f"ffmpeg PCM failed: {err[:300]}")

        if not pcm_data or len(pcm_data) < 32000:  # <1s of audio
            # Could be silent or very short; check if we got at least some data
            if len(pcm_data) == 0:
                raise RuntimeError("no PCM data (silent or no audio)")

        # PCM is s16le mono 16kHz: 2 bytes per sample, 16000 samples per second
        sample_rate = 16000
        bytes_per_sample = 2
        samples_per_window = sample_rate  # 1s
        bytes_per_window = samples_per_window * bytes_per_sample

        # Convert bytes to int16 via struct (streaming, not loading all at once if we chunk)
        # For efficiency, use memoryview and struct unpack in windows
        total_samples = len(pcm_data) // bytes_per_sample
        total_windows = min(n, total_samples // samples_per_window + (1 if total_samples % samples_per_window else 0))
        # If file shorter than duration, pad remaining windows as silence
        rms_raw: list[float] = []
        peak_raw: list[float] = []
        silence_raw: list[float] = []

        # Silence threshold: amplitude < 0.02 * max (roughly -34dB) counts as silent sample
        silence_thresh = 0.02 * 32768  # ~655

        for w in range(n):
            start_byte = w * bytes_per_window
            end_byte = start_byte + bytes_per_window
            chunk = pcm_data[start_byte:end_byte]
            if len(chunk) < bytes_per_window * 0.5:  # less than half window → pad as silence
                # Missing data at end of file
                rms_raw.append(0.0)
                peak_raw.append(0.0)
                silence_raw.append(1.0)
                continue
            # Unpack s16le
            # Use struct iterative for speed; for 16k samples per window, this is okay
            # Use memory efficient: struct.unpack with format
            fmt = f"<{len(chunk)//2}h"
            try:
                samples = struct.unpack(fmt, chunk)
            except struct.error:
                samples = [0] * (len(chunk)//2)

            # Normalize to -1..1 for RMS
            # Compute RMS, peak, silence ratio in one pass (no numpy to avoid dependency)
            sum_sq = 0.0
            peak = 0
            silent_count = 0
            for s in samples:
                # RMS uses normalized float
                norm = s / 32768.0
                sum_sq += norm * norm
                abs_s = abs(s)
                if abs_s > peak:
                    peak = abs_s
                if abs_s < silence_thresh:
                    silent_count += 1

            rms = math.sqrt(sum_sq / max(len(samples), 1))  # 0..1
            peak_norm = peak / 32768.0  # 0..1
            silence_ratio = silent_count / max(len(samples), 1)  # 0..1

            rms_raw.append(rms)
            peak_raw.append(peak_norm)
            silence_raw.append(silence_ratio)

        # Per-video normalization (percentile-based for loudness, z-score for burst)
        # Loudness: combine RMS (70%) + peak (30%) then percentile normalize
        # Compute per-video stats for debugging
        p50 = _percentile(rms_raw, 50)
        p75 = _percentile(rms_raw, 75)
        p90 = _percentile(rms_raw, 90)
        p95 = _percentile(rms_raw, 95)
        max_rms = max(rms_raw) if rms_raw else 0
        logger.info(f"Audio PCM: {len(rms_raw)} windows, p50={p50:.3f} p75={p75:.3f} p90={p90:.3f} p95={p95:.3f} max={max_rms:.3f}")

        # Silent video guard: if max RMS <0.01 (~ -40dB), treat as silence
        if max_rms < 0.01:
            logger.info("Audio PCM: near-silence detected (max RMS <0.01)")
            return {"loudness": [0.0]*n, "burst": [0.0]*n, "silence": [1.0]*n,
                    "_debug": {"p50": p50, "p95": p95, "max": max_rms, "time": 0, "source": "waveform_silent"}}

        # Loudness: use RMS directly, normalized per-video via percentile stretch
        # Map RMS 0..max to 0..1 via p95 scaling, then z-normalize for comparability
        # p95 is more robust than max against transient spikes (mic bump)
        scale = max(p95, 0.01)  # avoid div0; if p95 is 0 (silent), use 0.01
        loud_lin = [min(1.0, r / (scale * 1.2)) for r in rms_raw]  # 1.2x headroom
        # Also mix peak slightly
        loud_lin = [0.7 * l + 0.3 * p for l, p in zip(loud_lin, peak_raw)]
        # Z-normalize for soft ranking
        loud_norm = _normalize_per_video(loud_lin)

        # Burst: energy delta (positive jump), guard against isolated spikes
        burst_raw: list[float] = [0.0] * n
        for i in range(1, n):
            # Delta relative to p50, not absolute, so quiet podcast still shows burst
            delta = loud_lin[i] - loud_lin[i-1]
            # Only positive deltas, scaled
            if delta > 0:
                # Require that current loudness is above p50 (not just silence→slightly less silence)
                if loud_lin[i] > (p50 + 0.05):
                    burst_raw[i] = delta * 2.0  # amplify
                else:
                    burst_raw[i] = 0.0
            # Guard: isolated single-sample spike without surrounding speech → likely mic bump
            # Check if next window is also not loud (spike then immediate drop)
            if i < n-1 and burst_raw[i] > 0.3:
                if loud_lin[i+1] < p50:
                    # Spike then silence → likely artifact, dampen
                    burst_raw[i] *= 0.3

        burst_norm = _normalize_per_video(burst_raw)

        # Silence mask: 1.0 where silence_ratio >0.9 or RMS very low
        # Use absolute RMS threshold (0.01 ≈ -40dB) instead of p5-relative,
        # which falsely marks constant tone as silent when p5≈max.
        silence_mask = [0.0] * n
        for i in range(n):
            if silence_raw[i] > 0.9 or rms_raw[i] < 0.01:
                silence_mask[i] = 1.0
            elif silence_raw[i] > 0.7 or rms_raw[i] < 0.02:
                silence_mask[i] = 0.5

        t1 = time.time()
        logger.info(f"Audio signals: REAL waveform, {n} windows, {len([b for b in burst_norm if b>0.6])} burst peaks, "
                    f"{sum(silence_mask):.0f} silent seconds, time={t1-t0:.2f}s")

        return {"loudness": loud_norm, "burst": burst_norm, "silence": silence_mask,
                "_debug": {"p50": p50, "p95": p95, "max": max_rms, "time": t1-t0, "source": "waveform"}}

    except Exception as exc:
        # Fallback: try to use volumedetect mean as flat, but log explicitly
        logger.warning(f"Audio waveform extraction failed ({exc}) — using fallback")
        # Try volumedetect fallback for at least mean
        try:
            result = subprocess.run(
                [resolve_ffmpeg(), "-t", str(min(duration, 30)), "-i", video_path,
                 "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True, text=True, timeout=15,
            )
            m = re.search(r"mean_volume:\s*([-+]?\d+(?:\.\d+)?)\s*dB", result.stderr or "")
            if m:
                v = float(m.group(1))
                # Create flat loudness based on mean (not ideal, but not fake)
                # Use word-density proxy if available? For now flat
                flat = max(0.0, (v + 60) / 60)
                loud = [flat] * n
                logger.info(f"Audio fallback: volumedetect mean {v:.1f}dB → flat {flat:.2f}")
                return {"loudness": loud, "burst": [0.0]*n, "silence": [0.0]*n,
                        "_debug": {"source": "fallback_volumedetect", "mean_db": v}}
        except Exception as exc2:
            logger.debug(f"volumedetect fallback also failed: {exc2}")

        # Ultimate fallback: explicit fake, logged
        logger.warning(f"Audio signals: FALLBACK (fake 0.5) for {os.path.basename(video_path)}")
        return {"loudness": [0.5]*n, "burst": [0.0]*n, "silence": [0.0]*n,
                "_debug": {"source": "fallback_fake"}}


def _has_audio_stream(video_path: str) -> bool:
    try:
        result = subprocess.run(
            [resolve_ffprobe(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=15,
        )
        return bool(result.stdout.strip())
    except Exception:
        return True  # assume exists


def _detect_silences(video_path: str, duration: float,
                     noise_db: int = -35, min_dur: float = 0.4) -> list[tuple[float, float]]:
    """Wrapper around retention-style silencedetect, returns absolute pairs."""
    try:
        result = subprocess.run(
            [resolve_ffmpeg(), "-t", str(duration), "-i", video_path,
             "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", result.stderr or "")]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", result.stderr or "")]
        pairs = list(zip(starts, ends))
        return [(s, e) for s, e in pairs if e > s and s < duration]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Visual — motion + scene cuts (ffmpeg-based, no ML)
# ---------------------------------------------------------------------------

def extract_visual_signals(video_path: str, duration: float) -> dict[str, list[float]]:
    """
    Returns {motion, scene_cut} per-second 0-1 lists.
    motion: frame-difference magnitude via mpdecimate-like select.
    scene_cut: 1.0 at detected scene boundaries (select='gt(scene,0.4)').
    """
    n = max(1, math.ceil(duration))
    motion = _visual_motion_per_second(video_path, duration, n)
    cuts = _scene_cuts(video_path, duration)
    scene_mask = [0.0] * n
    for t in cuts:
        idx = max(0, min(n - 1, int(t)))
        scene_mask[idx] = 1.0
        if idx + 1 < n:
            scene_mask[idx + 1] = max(scene_mask[idx + 1], 0.5)
    return {"motion": motion, "scene_cut": scene_mask}


_visual_cache: dict[str, dict] = {}

def _visual_motion_per_second(video_path: str, duration: float, n: int) -> list[float]:
    """
    Fast motion via ffprobe packet sizes (demux only, no decode) — 160x90 not needed,
    packet size is a strong motion proxy and ~10x faster than signalstats.
    Falls back to signalstats at 160x90, 1 FPS if ffprobe fails.
    """
    # Try packet-size method first (1 FPS equivalent via per-second bucket)
    try:
        # Cache key includes mtime
        mtime = os.path.getmtime(video_path) if os.path.exists(video_path) else 0
        cache_key = f"{video_path}:{mtime}:{duration}:motion"
        if cache_key in _visual_cache:
            return _visual_cache[cache_key]
        result = subprocess.run(
            [resolve_ffprobe(), "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,size,flags",
             "-of", "csv=p=0",
             "-read_intervals", f"0%+{duration}",
             video_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Bucket packet sizes per second
            per_sec = [0] * n
            counts = [0] * n
            for line in result.stdout.splitlines():
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    pts = float(parts[0])
                    size = int(parts[1])
                    idx = min(n-1, max(0, int(pts)))
                    per_sec[idx] += size
                    counts[idx] += 1
                except: continue
            # Average size per packet in that second (or 0 if no packets)
            avg = [per_sec[i]/counts[i] if counts[i] else 0 for i in range(n)]
            maxv = max(avg) if avg else 1.0
            if maxv > 0:
                lin = [v/maxv for v in avg]
                norm = _normalize_per_video(lin)
                _visual_cache[cache_key] = norm
                return norm
    except Exception as exc:
        logger.debug(f"packet motion failed: {exc}")

    # Fallback: lightweight signalstats at 160x90, 1 FPS
    try:
        result = subprocess.run(
            [resolve_ffmpeg(), "-t", str(duration), "-i", video_path,
             "-vf", "fps=1,scale=160:90,signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        vals: list[float] = []
        for line in (result.stderr or "").splitlines():
            m = re.search(r"YDIF.*?([0-9.]+)", line)
            if m:
                vals.append(float(m.group(1)))
        if vals:
            chunk = max(1, len(vals) // max(n, 1))
            per_sec: list[float] = []
            for i in range(n):
                sl = vals[i * chunk:(i + 1) * chunk]
                per_sec.append(sum(sl) / len(sl) if sl else 0.0)
            maxv = max(per_sec) if per_sec else 1.0
            if maxv < 1e-9:
                return [0.0] * n
            lin = [v / maxv for v in per_sec]
            return _normalize_per_video(lin)
    except Exception as exc:
        logger.debug("visual motion extract failed: %s", exc)
    return [0.5] * n


def _scene_cuts(video_path: str, duration: float, thresh: float = 0.4) -> list[float]:
    """Detect scene cuts via ffprobe keyframes (fast, no decode) — K flag + size."""
    # Try fast ffprobe keyframe method first
    try:
        result = subprocess.run(
            [resolve_ffprobe(), "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,flags,size",
             "-of", "csv=p=0",
             "-read_intervals", f"0%+{duration}",
             video_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            times: list[float] = []
            prev_pts = -10.0
            for line in result.stdout.splitlines():
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    pts = float(parts[0])
                    flags = parts[1] if len(parts) > 1 else ""
                    # size = int(parts[2]) if len(parts) > 2 else 0  # not needed for now
                    if "K" in flags and pts - prev_pts > 1.0:  # keyframe at least 1s apart
                        times.append(pts)
                        prev_pts = pts
                except: continue
            # Filter to duration and return
            return [t for t in times if 0 <= t < duration]
    except Exception as exc:
        logger.debug(f"keyframe scene detection failed: {exc}")

    # Fallback: lightweight ffmpeg scene at 160x90, 1 FPS
    try:
        result = subprocess.run(
            [resolve_ffmpeg(), "-t", str(duration), "-i", video_path,
             "-filter:v", f"fps=1,scale=160:90,select='gt(scene,{thresh})',showinfo",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        times: list[float] = []
        for line in (result.stderr or "").splitlines():
            m = re.search(r"pts_time:([0-9.]+)", line)
            if m and "showinfo" in line:
                times.append(float(m.group(1)))
        return [t for t in times if 0 <= t < duration]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Face-derived signals (from already-extracted face_data)
# ---------------------------------------------------------------------------

def extract_face_signals(face_data: list[list[dict]], duration: float) -> dict[str, list[float]]:
    """
    face_data: per-second list of faces [{cx,cy,size}].

    Returns {face_presence, face_size, face_size_delta}
    """
    n = max(1, math.ceil(duration))
    if len(face_data) < n:
        face_data = face_data + [[]] * (n - len(face_data))
    presence = [1.0 if len(sec) > 0 else 0.0 for sec in face_data[:n]]
    size = []
    for sec in face_data[:n]:
        if sec:
            size.append(max(f["size"] for f in sec))
        else:
            size.append(0.0)
    nz = [v for v in size if v > 0]
    if nz:
        nz_norm = _normalize_per_video(nz)
        norm_map = {}
        j = 0
        for i, v in enumerate(size):
            if v > 0:
                norm_map[i] = nz_norm[j]
                j += 1
            else:
                norm_map[i] = 0.0
        size = [norm_map[i] for i in range(n)]
    delta = [0.0] * n
    for i in range(1, n):
        d = size[i] - size[i - 1]
        delta[i] = max(0.0, d * 2.0)
    delta = _normalize_per_video(delta) if any(delta) else delta
    return {"face_presence": presence, "face_size": size, "face_size_delta": delta}


def build_reaction_signal(audio_burst: list[float], face_delta: list[float],
                          motion: list[float]) -> list[float]:
    """
    Fused reaction: audio burst + face-size jump + motion spike.
    Normalized per-video. Captures laughter, surprise, hype.
    """
    n = max(len(audio_burst), len(face_delta), len(motion))
    if n == 0:
        return []
    def _pad(lst: list[float]) -> list[float]:
        if len(lst) < n:
            return lst + [lst[-1] if lst else 0.0] * (n - len(lst))
        return lst[:n]
    ab = _pad(audio_burst)
    fd = _pad(face_delta)
    mo = _pad(motion)
    fused = [0.5 * ab[i] + 0.3 * fd[i] + 0.2 * mo[i] for i in range(n)]
    return _normalize_per_video(fused)
