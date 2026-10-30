"""Per-clip output planner.

Picks the framing strategy + caption style for one clip so we never publish a
mushy over-cropped 9:16 clip, and never crop the screen out of a tutorial.

Strategies
----------
crop       tracked 9:16 face crop (large, well-framed face survives upscale)
blur_fill  16:9 frame centred on a blurred 9:16 canvas (tutorial / no face)
letterbox  keep the source aspect (16:9) on a 16:9 canvas
"""

from __future__ import annotations

import os
import sys
from typing import Any

try:
    import layout_detector
except ImportError:  # pragma: no cover - direct module invocation
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import layout_detector  # type: ignore

DEFAULT_STRATEGY = "blur_fill"

CAPTION_STYLES: dict[str, str] = {
    "crop": "viral_word",
    "blur_fill": "minimal_clean",
    "letterbox": "minimal_clean",
}

# Explicit user overrides -> (strategy, aspect). "auto" lets the detector decide.
OVERRIDES: dict[str, tuple[str, str]] = {
    "crop_9x16": ("crop", "9:16"),
    "crop_1x1": ("crop", "1:1"),
    "blur_9x16": ("blur_fill", "9:16"),
    "native_16x9": ("letterbox", "16:9"),
}

# A face at least this large (fraction of frame height) can be cropped to 9:16
# without blowing the upscale budget.
MIN_CROP_FACE = 0.18
# Even a large-ish webcam face is kept small when a screen region is present;
# the screen is the content, so switch to the blurred backdrop instead.
SCREEN_KEEP_FACE = 0.25
# Below this much face presence the framing is unstable -> blur-fill.
MIN_FACE_PRESENCE = 0.15
# A source this tall (or taller) already is vertical.
PORTRAIT_AR = 1.05
# Two-speaker content: keep both people by filling rather than cropping one out.
MULTI_SPEAKER_RATIO = 0.5


def _face_stats(face_data: list[list[dict[str, Any]]]) -> tuple[float, float, float]:
    """Return (avg_face_size, face_presence, multi_speaker_ratio)."""
    sizes = [
        float(f.get("size", 0.0))
        for sec in (face_data or [])
        for f in (sec or [])
        if f.get("size")
    ]
    total = max(len(face_data or []), 1)
    face_frames = sum(1 for sec in (face_data or []) if sec)
    multi_frames = sum(1 for sec in (face_data or []) if len(sec or []) >= 2)
    avg_size = sum(sizes) / len(sizes) if sizes else 0.0
    return avg_size, face_frames / total, multi_frames / total


def _result(strategy: str, aspect: str, layout_mode: str,
            has_screen: bool, avg_size: float, reason: str,
            output_mode: str = "auto") -> dict[str, Any]:
    if strategy == "letterbox":
        canvas = aspect
    else:
        canvas = "1:1" if aspect == "1:1" else "9:16"
    return {
        "strategy": strategy,
        "aspect": aspect,
        "canvas": canvas,
        "caption_style": CAPTION_STYLES.get(strategy, "minimal_clean"),
        "layout_mode": layout_mode,
        "has_screen_region": bool(has_screen),
        "avg_face_size": round(float(avg_size), 3),
        "reason": reason,
        "output_mode": output_mode,
    }


def plan_clip_output(source_w: int, source_h: int, frames: list[Any],
                     face_data: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Decide the best framing + captions for a clip from its analysis."""
    w = max(int(source_w or 1), 1)
    h = max(int(source_h or 1), 1)
    aspect_ratio = w / h
    avg_size, presence, multi = _face_stats(face_data)

    if aspect_ratio <= PORTRAIT_AR:
        # Already vertical / square: crop is lossless-ish, no fake blur needed.
        aspect = "1:1" if aspect_ratio > 0.9 else "9:16"
        return _result("crop", aspect, "single", False, avg_size,
                       "source_already_vertical")

    try:
        layout = layout_detector.detect_layout(frames or [], face_data or [], w, h)
    except Exception:
        layout = {"mode": "none", "has_screen_region": False, "face_count": 0}

    layout_mode = str(layout.get("mode", "none"))
    has_screen = bool(layout.get("has_screen_region"))

    if layout_mode == "none":
        return _result(DEFAULT_STRATEGY, "9:16", layout_mode, has_screen,
                       avg_size, "no_reliable_face")
    if layout_mode == "tutorial":
        return _result(DEFAULT_STRATEGY, "9:16", layout_mode, has_screen,
                       avg_size, "tutorial_layout")
    if presence < MIN_FACE_PRESENCE:
        return _result(DEFAULT_STRATEGY, "9:16", layout_mode, has_screen,
                       avg_size, "no_reliable_face")
    if has_screen and avg_size < SCREEN_KEEP_FACE:
        return _result(DEFAULT_STRATEGY, "9:16", layout_mode, has_screen,
                       avg_size, "screen_content")
    if multi > MULTI_SPEAKER_RATIO:
        return _result(DEFAULT_STRATEGY, "9:16", layout_mode, has_screen,
                       avg_size, "multi_speaker")
    if avg_size >= MIN_CROP_FACE:
        return _result("crop", "9:16", layout_mode, has_screen,
                       avg_size, "large_face")
    return _result(DEFAULT_STRATEGY, "9:16", layout_mode, has_screen,
                   avg_size, "small_face")


def resolve_plan(output_mode: str | None, source_w: int, source_h: int,
                 frames: list[Any], face_data: list[list[dict[str, Any]]]
                 ) -> dict[str, Any]:
    """Apply an explicit user override, else auto-detect the output plan."""
    key = str(output_mode or "auto").strip().lower()
    override = OVERRIDES.get(key)
    if override is None:
        return plan_clip_output(source_w, source_h, frames, face_data)

    strategy, aspect = override
    base = plan_clip_output(source_w, source_h, frames, face_data)
    return _result(strategy, aspect, base.get("layout_mode", "none"),
                   base.get("has_screen_region", False),
                   base.get("avg_face_size", 0.0),
                   f"user_override:{key}", output_mode=key)
