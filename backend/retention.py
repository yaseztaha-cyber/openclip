"""
retention.py — Pacing edits: squeeze dead air, drop fillers, re-join.

Works on the RAW cut clip BEFORE reframing/captions so tracking and
subtitles follow the edited timeline. All cuts are re-encoded
(frame-accurate); kept spans are joined with the concat demuxer.

Guardrails (from autoclip-core): never remove >40% of a clip, keep spans
>= 0.3s, merge cuts within 0.2s. If anything looks off, returns the input
untouched — a boring edit beats a broken one.
"""

import logging
import os
import re
import subprocess
import tempfile
from typing import Optional

try:
    from ffmpeg_utils import resolve_ffmpeg
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ffmpeg_utils import resolve_ffmpeg

import editorial

logger = logging.getLogger(__name__)


def detect_silences(video_path: str, start: float = 0.0,
                    duration: Optional[float] = None,
                    noise_db: int = -35, min_dur: float = 0.5) -> list[tuple[float, float]]:
    """
    Run silencedetect over [start, start+duration]. Returns absolute
    (silence_start, silence_end) pairs, clipped to the window.
    """
    cmd = [resolve_ffmpeg()]
    if duration is not None:
        cmd += ["-ss", str(max(0.0, start)), "-t", str(duration)]
    cmd += ["-i", video_path, "-af",
            f"silencedetect=noise={noise_db}dB:d={min_dur}",
            "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as exc:
        logger.warning("silencedetect failed: %s", exc)
        return []
    stderr = result.stderr or ""
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", stderr)]
    pairs = list(zip(starts, ends))
    base = start if duration is not None else 0.0
    out: list[tuple[float, float]] = []
    for s, e in pairs:
        s, e = s + base, e + base
        if e > s:
            out.append((s, e))
    return out


def plan_retention_cuts(words: list[dict], silences: list[tuple[float, float]],
                        clip_start: float, clip_end: float) -> list[dict]:
    """
    Build the cut list for one clip window (absolute seconds):
    filler tokens + squeezed pauses, merged + guardrailed.
    """
    duration = clip_end - clip_start
    if duration <= 0:
        return []
    window_words = [w for w in words
                    if clip_start - 0.5 <= float(w.get("start", 0.0)) < clip_end + 0.5]
    cuts = editorial.detect_filler_cuts(window_words, duration)
    # Only squeeze silences fully inside the window.
    inner = [(max(s, clip_start), min(e, clip_end)) for s, e in silences
             if e > clip_start and s < clip_end]
    inner = [(s, e) for s, e in inner if e - s >= editorial.PAUSE_SQUEEZE_MIN_SEC]
    cuts += editorial.plan_pause_cuts(inner)
    cuts = [c for c in cuts if c["end"] > clip_start and c["start"] < clip_end]
    for c in cuts:
        c["start"] = max(c["start"], clip_start)
        c["end"] = min(c["end"], clip_end)
    return editorial.apply_guardrails(cuts, duration)


def render_edited_clip(raw_clip: str, cuts: list[dict], out_path: str,
                       duration: float) -> tuple[str, float]:
    """
    Cut out `cuts` (absolute raw-clip seconds) and join the keeps.

    Returns (final_path, removed_seconds). With no cuts, stream-copies.
    With cuts, re-encodes (quality args) for frame accuracy.
    """
    cuts = editorial.merge_cuts(cuts)
    if not cuts:
        # No cuts: fast stream copy.
        result = subprocess.run(
            [resolve_ffmpeg(), "-y", "-i", raw_clip, "-c", "copy", out_path],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Retention copy failed: {result.stderr[-500:]}")
        return out_path, 0.0

    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for c in cuts:
        if c["start"] - cursor >= editorial.MIN_KEEP_SEC:
            keeps.append((cursor, c["start"]))
        cursor = max(cursor, c["end"])
    if duration - cursor >= editorial.MIN_KEEP_SEC:
        keeps.append((cursor, duration))
    if not keeps:
        raise RuntimeError("Retention plan keeps nothing — refusing edit")

    removed = sum(c["end"] - c["start"] for c in cuts)
    tmpdir = tempfile.mkdtemp(prefix="retention_")
    try:
        parts: list[str] = []
        for i, (s, e) in enumerate(keeps):
            part = os.path.join(tmpdir, f"part{i:03d}.mp4")
            result = subprocess.run(
                [resolve_ffmpeg(), "-y", "-ss", f"{s:.3f}", "-i", raw_clip,
                 "-t", f"{e - s:.3f}",
                 "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-avoid_negative_ts", "make_zero", part],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Retention cut {i} failed: {result.stderr[-500:]}")
            parts.append(part)

        if len(parts) == 1:
            os.replace(parts[0], out_path)
        else:
            lst = os.path.join(tmpdir, "concat.txt")
            with open(lst, "w") as f:
                for p in parts:
                    f.write(f"file '{p}'\n")
            result = subprocess.run(
                [resolve_ffmpeg(), "-y", "-f", "concat", "-safe", "0",
                 "-i", lst, "-c", "copy", out_path],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Retention join failed: {result.stderr[-500:]}")
        return out_path, removed
    finally:
        for root, _d, files in os.walk(tmpdir):
            for fn in files:
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass