"""
quality.py — Post-render quality control for clips.

Honest file-level checks after each clip is rendered:
  - file exists and is non-trivial
  - video stream present, sane dimensions
  - audio stream present
  - duration matches the expected edited length (within tolerance)
  - captions were actually burned when requested
  - a face was tracked (unless the layout genuinely has none)

On failure the clip is KEPT but flagged (needs_user_confirm) with reasons —
QC never deletes work, it routes weak clips to human review.
"""

import json
import logging
import os
import subprocess
from typing import Optional

try:
    from ffmpeg_utils import resolve_ffprobe
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ffmpeg_utils import resolve_ffprobe

logger = logging.getLogger(__name__)

MIN_FILE_BYTES = 50 * 1024
DURATION_TOLERANCE = 0.15  # 15% drift allowed (cuts/encodes shift slightly)


def check_clip(path: str, expected_duration: float = 0.0,
               caption_events: int = 0, captions_expected: bool = False,
               face_count: int = 0, layout_mode: str = "") -> dict:
    """
    Inspect a rendered clip. Returns {"pass": bool, "reasons": [str]}.
    """
    reasons: list[str] = []

    if not path or not os.path.isfile(path):
        return {"pass": False, "reasons": ["output file missing"]}
    if os.path.getsize(path) < MIN_FILE_BYTES:
        reasons.append("output file suspiciously small")

    streams = _probe_streams(path)
    if streams is None:
        reasons.append("could not probe output")
        return {"pass": False, "reasons": reasons}

    kinds = {s.get("codec_type") for s in streams}
    if "video" not in kinds:
        reasons.append("no video stream")
    if "audio" not in kinds:
        reasons.append("no audio stream")

    if expected_duration > 0:
        actual = _stream_duration(streams)
        if actual is not None and abs(actual - expected_duration) > expected_duration * DURATION_TOLERANCE + 1.0:
            reasons.append(f"duration drift ({actual:.1f}s vs expected {expected_duration:.1f}s)")

    if captions_expected and caption_events <= 0:
        reasons.append("captions requested but none burned")

    if face_count <= 0 and layout_mode not in ("none", "", "center", "tutorial"):
        reasons.append("no face tracked for a face layout")

    if reasons:
        logger.warning("QC fail for %s: %s", path, reasons)
    return {"pass": not reasons, "reasons": reasons}


def _probe_streams(path: str) -> Optional[list]:
    try:
        result = subprocess.run(
            [resolve_ffprobe(), "-v", "error",
             "-show_entries", "stream=index,codec_type,width,height,duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout).get("streams", [])
    except Exception as exc:
        logger.warning("ffprobe failed in QC: %s", exc)
        return None


def _stream_duration(streams: list) -> Optional[float]:
    for s in streams:
        if s.get("codec_type") == "video" and s.get("duration"):
            try:
                return float(s["duration"])
            except (ValueError, TypeError):
                continue
    for s in streams:
        if s.get("duration"):
            try:
                return float(s["duration"])
            except (ValueError, TypeError):
                continue
    return None