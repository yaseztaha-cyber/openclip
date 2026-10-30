import asyncio
import subprocess
import json
import os
import math
import re as _re
import sys
import types
import threading
from PIL import Image  # type: ignore

# mediapipe internally imports cv2 only for its drawing utilities (drawing_styles.py ->
# drawing_utils.py). Our code never calls any drawing functions. We pre-stub cv2 in
# sys.modules so the native OpenCV binary (and its libGL dependency) is never loaded.
if 'cv2' not in sys.modules:
    _cv2_stub = types.ModuleType('cv2')
    sys.modules['cv2'] = _cv2_stub

import mediapipe as mp  # type: ignore
from mediapipe.tasks import python  # type: ignore
from mediapipe.tasks.python import vision  # type: ignore
import io
from typing import Optional, Any, Callable
import logging

try:
    from ffmpeg_utils import resolve_ffmpeg, resolve_ffprobe
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ffmpeg_utils import resolve_ffmpeg, resolve_ffprobe

try:
    import retention as _retention_mod
    import editorial as _editorial_mod
    import signals as _signals_mod
    import composition as _composition_mod
    import auto_output as _auto_output_mod
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import retention as _retention_mod
    import editorial as _editorial_mod
    import signals as _signals_mod
    import composition as _composition_mod
    import auto_output as _auto_output_mod

logger = logging.getLogger(__name__)

_VALID_LAYOUTS = {"single", "tutorial", "podcast", "panel", "none", "center"}

# aspect -> (w_ratio, h_ratio, out_w, out_h)
ASPECTS: dict[str, tuple[float, float, int, int]] = {
    "9:16": (9.0, 16.0, 1080, 1920),
    "1:1": (1.0, 1.0, 1080, 1080),
    "16:9": (16.0, 9.0, 1920, 1080),
    "4:3": (4.0, 3.0, 1440, 1080),
}
DEFAULT_ASPECT = "9:16"

# Never upscale a crop beyond this factor when rendering to the output size.
# Above it, 1080x1920 looks mushy — better to widen the crop (more of the
# source stays visible and sharp) than to zoom in past the source pixels.
MAX_UPSCALE = float(os.environ.get("MAX_UPSCALE", "1.5"))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def _upscale_cap_zoom(src_h: int, out_h: int) -> float:
    """Largest zoom that keeps output upscale within MAX_UPSCALE."""
    if src_h <= 0 or out_h <= 0:
        return 3.0
    return max(1.0, MAX_UPSCALE * float(src_h) / float(out_h))


def _aspect_spec(aspect: str | None) -> tuple[float, float, int, int]:
    """Normalise an aspect label to (ratio_w, ratio_h, out_w, out_h)."""
    key = str(aspect or "").strip().lower().replace(" ", "")
    aliases = {"9x16": "9:16", "1x1": "1:1", "16x9": "16:9", "4x3": "4:3",
               "vertical": "9:16", "square": "1:1", "wide": "16:9", "landscape": "16:9"}
    return ASPECTS.get(aliases.get(key, key), ASPECTS[DEFAULT_ASPECT])


def _compute_crop(vid_width: int, vid_height: int, zoom: float, cx: float, cy: float,
                  ar_w: float, ar_h: float, x_offset: float, y_offset: float
                  ) -> tuple[int, int, int, int]:
    """Crop rect for an aspect ratio at a zoom + center + manual offsets.

    Rounds (not truncates) and forces even dimensions/offsets so yuv420p
    chroma stays aligned. Keeps preview/render parity — frontend must mirror.
    """
    crop_h = float(vid_height) / max(zoom, 1e-6)
    crop_w = crop_h * ar_w / ar_h
    if crop_w > vid_width:
        crop_w = float(vid_width)
        crop_h = crop_w * ar_h / ar_w
    if crop_h > vid_height:
        crop_h = float(vid_height)
        crop_w = crop_h * ar_w / ar_h

    crop_w_i = max(2, int(round(crop_w)) & ~1)
    crop_h_i = max(2, int(round(crop_h)) & ~1)
    crop_w_i = min(crop_w_i, int(vid_width) & ~1 or 2)
    crop_h_i = min(crop_h_i, int(vid_height) & ~1 or 2)

    slack_x = float(vid_width) - crop_w_i
    slack_y = float(vid_height) - crop_h_i
    fx = _clamp(cx - crop_w_i / 2.0 + x_offset * slack_x * 0.5, 0.0, max(slack_x, 0.0))
    fy = _clamp(cy - crop_h_i / 2.0 + y_offset * slack_y * 0.5, 0.0, max(slack_y, 0.0))
    crop_x_i = int(round(fx)) & ~1
    crop_y_i = int(round(fy)) & ~1
    crop_x_i = int(_clamp(crop_x_i, 0, max(0, int(vid_width) - crop_w_i)))
    crop_y_i = int(_clamp(crop_y_i, 0, max(0, int(vid_height) - crop_h_i)))
    # re-even after clamp (clamp bound may be odd)
    if crop_x_i + crop_w_i > vid_width:
        crop_x_i = (int(vid_width) - crop_w_i) & ~1
    if crop_y_i + crop_h_i > vid_height:
        crop_y_i = (int(vid_height) - crop_h_i) & ~1
    return crop_w_i, crop_h_i, max(0, crop_x_i), max(0, crop_y_i)


def _validate_keyframes(keyframes, duration: float) -> list[dict]:
    """Validate, clamp, sort and dedupe keyframes. Returns normalized list."""
    if not keyframes:
        return []
    cleaned: list[dict] = []
    seen_t: dict[float, dict] = {}
    for kf in keyframes:
        try:
            t = float(kf.get("t", kf.get("time", 0)))
            x = float(kf.get("x", 0.5))
            y = float(kf.get("y", 0.5))
            zoom = float(kf.get("zoom", 1.0))
        except Exception:
            continue
        # Clamp
        t = max(0.0, min(t, max(0.0, duration)))
        x = _clamp(x, 0.0, 1.0)
        y = _clamp(y, 0.0, 1.0)
        zoom = _clamp(zoom, 0.5, 3.0)
        # Dedupe: last wins for same t
        seen_t[round(t, 3)] = {"t": t, "x": x, "y": y, "zoom": zoom}
    # Sort by t
    cleaned = sorted(seen_t.values(), key=lambda k: k["t"])
    # Ensure at least one keyframe covers t=0 if needed: if first t >0.1, keep as is (before first uses first)
    return cleaned


def _interpolate_keyframes(keyframes: list[dict], t: float, duration: float) -> tuple[float, float, float]:
    """
    Linear interpolate x/y/zoom at time t.
    Returns (x, y, zoom) normalized 0..1 / 0.5..3.
    Single source of truth for preview and render.
    """
    if not keyframes:
        return 0.5, 0.5, 1.0
    kfs = _validate_keyframes(keyframes, duration)
    if not kfs:
        return 0.5, 0.5, 1.0
    if len(kfs) == 1:
        return kfs[0]["x"], kfs[0]["y"], kfs[0]["zoom"]
    # Before first
    if t <= kfs[0]["t"]:
        return kfs[0]["x"], kfs[0]["y"], kfs[0]["zoom"]
    # After last
    if t >= kfs[-1]["t"]:
        return kfs[-1]["x"], kfs[-1]["y"], kfs[-1]["zoom"]
    # Find segment
    for i in range(len(kfs) - 1):
        t0, t1 = kfs[i]["t"], kfs[i+1]["t"]
        if t0 <= t <= t1:
            if t1 - t0 < 1e-6:
                return kfs[i+1]["x"], kfs[i+1]["y"], kfs[i+1]["zoom"]
            alpha = (t - t0) / (t1 - t0)
            alpha = _clamp(alpha, 0.0, 1.0)
            x = kfs[i]["x"] + alpha * (kfs[i+1]["x"] - kfs[i]["x"])
            y = kfs[i]["y"] + alpha * (kfs[i+1]["y"] - kfs[i]["y"])
            zoom = kfs[i]["zoom"] + alpha * (kfs[i+1]["zoom"] - kfs[i]["zoom"])
            return x, y, zoom
    return kfs[-1]["x"], kfs[-1]["y"], kfs[-1]["zoom"]


def _encode_args() -> list[str]:
    """High-quality H264 output that starts fast in browsers/phones."""
    return ["-c:v", "libx264", "-crf", "17", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def _zoom_for_size(size: float) -> float:
    """Face-size (fraction of frame height) -> base zoom.

    Capped so upscale never exceeds ~2x (beyond that 1080x1920 looks mushy).
    """
    if size < 0.10:
        table = 2.0
    elif size < 0.20:
        table = 1.8
    elif size < 0.35:
        table = 1.5
    else:
        table = 1.25
    # Fit rule: the face should fill at most ~38% of the crop height.
    fit = 0.38 / max(size, 1e-6)
    return _clamp(min(table, fit), 1.0, 2.2)


def _has_audio(video_path: str) -> bool:
    """True if the file contains an audio stream."""
    try:
        ffprobe_path = resolve_ffprobe()
        result = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return True


def _audio_args(video_path: str) -> list[str]:
    """Loudness-normalized AAC audio (modern short-form loudness)."""
    if _has_audio(video_path):
        return ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2"]
    return []


def _grade_suffix(options: dict) -> str:
    """Subtle color punch so phone footage pops. Disable with grade=False."""
    if options.get("grade", True):
        return ",eq=saturation=1.07:contrast=1.02"
    return ""


def _detect_leading_silence(video_path: str, max_scan: float = 8.0) -> float:
    """Seconds of near-silence at the head of the clip (0.0 = starts on sound)."""
    try:
        ffmpeg_path = resolve_ffmpeg()
        result = subprocess.run(
            [ffmpeg_path, "-ss", "0", "-t", str(max_scan + 2), "-i", video_path,
             "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        stderr = result.stderr or ""
        starts = [float(x) for x in _re.findall(r"silence_start:\s*([\d.]+)", stderr)]
        ends = [float(x) for x in _re.findall(r"silence_end:\s*([\d.]+)", stderr)]
        if ends and (not starts or starts[0] < 0.3):
            return _clamp(ends[0], 0.0, max_scan)
        return 0.0
    except Exception as exc:
        logger.warning("Silence detect failed: %s", exc)
        return 0.0


def _trim_leading_silence(raw_clip: str, duration: float) -> float:
    """Cut dead air off the head of the raw clip in place. Returns seconds trimmed."""
    try:
        silence = _detect_leading_silence(raw_clip)
        if silence < 0.3 or silence > 6.0 or (duration - silence) < 15.0:
            return 0.0
        ffmpeg_path = resolve_ffmpeg()
        tmp_out = raw_clip + ".trimmed.mp4"
        # Re-encode (not copy) so the cut is frame-accurate — copy cuts freeze
        # on the first GOP and look broken.
        result = subprocess.run(
            [ffmpeg_path, "-y", "-ss", f"{silence:.3f}", "-i", raw_clip,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-avoid_negative_ts", "make_zero", tmp_out],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0 or not os.path.exists(tmp_out):
            return 0.0
        os.replace(tmp_out, raw_clip)
        logger.info("Hook trim: cut %.2fs of leading silence", silence)
        return silence
    except Exception as exc:
        logger.warning("Hook trim failed: %s", exc)
        return 0.0


def _estimate_content_center(frame: Image.Image, face_data_sec: list[dict],
                               vid_width: int, vid_height: int) -> tuple[float, float, float]:
    """
    Estimate dominant screen/content center for tutorial framing.
    Returns (cx, cy, area_ratio) where area_ratio is content area / frame area.
    Uses variance in 4x4 grid like layout_detector but returns center of
    high-variance non-face cells.
    """
    try:
        import numpy as np  # type: ignore
        gray = np.array(frame.convert('L'), dtype=float)
        h, w = gray.shape
        cell_h, cell_w = h // 4, w // 4
        face_mask = np.zeros((4, 4), dtype=bool)
        for face in face_data_sec:
            col = max(0, min(3, int(face['cx'] / w * 4)))
            row = max(0, min(3, int(face['cy'] / h * 4)))
            face_mask[row, col] = True
        high_cells: list[tuple[int, int]] = []
        for row in range(4):
            for col in range(4):
                if face_mask[row, col]:
                    continue
                cell = gray[row*cell_h:(row+1)*cell_h, col*cell_w:(col+1)*cell_w]
                if float(np.var(cell)) > 800:
                    high_cells.append((row, col))
        if not high_cells:
            return vid_width / 2.0, vid_height / 2.0, 0.0
        # Center of high-variance cells
        avg_row = sum(r for r, _ in high_cells) / len(high_cells)
        avg_col = sum(c for _, c in high_cells) / len(high_cells)
        cx = (avg_col + 0.5) * cell_w
        cy = (avg_row + 0.5) * cell_h
        area_ratio = len(high_cells) / 16.0
        return float(cx), float(cy), float(area_ratio)
    except Exception:
        return vid_width / 2.0, vid_height / 2.0, 0.0


def _resolve_layout(frames, face_data, vid_width: int, vid_height: int,
                    face_count: int, options: dict) -> str:
    """Decide crop strategy: forced option wins, else layout_detector."""
    forced = str(options.get("layout_mode") or "").strip().lower()
    if forced in _VALID_LAYOUTS:
        return forced
    try:
        from layout_detector import detect_layout  # type: ignore
        mode = str(detect_layout(frames, face_data, vid_width, vid_height).get("mode", ""))
        if mode in _VALID_LAYOUTS:
            return mode
    except Exception as exc:
        logger.warning("Layout detect failed: %s", exc)
    return "single" if face_count > 0 else "none"


def _punch_factor(t: float, every: float = 4.0, amount: float = 0.06,
                  trans: float = 0.6) -> float:
    """Subtle punch-in pulse: alternates 1.0x / (1+amount)x every `every` seconds."""
    seg = int(t // every)
    phase = (t % every) / every
    edge = trans / every
    cur = 1.0 if seg % 2 == 0 else 1.0 + amount
    if phase < edge and seg > 0:
        prev = 1.0 + amount if (seg - 1) % 2 == 1 else 1.0
        return prev + (cur - prev) * _ease_in_out(phase / edge)
    return cur


def grab_frame(video_path: str, at_seconds: float) -> Image.Image:
    """Grab a single full-resolution frame for the crop-preview UI."""
    ffmpeg_path = resolve_ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg_path, "-ss", str(max(0.0, at_seconds)), "-i", video_path,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"ffmpeg not found at '{ffmpeg_path}'.") from e
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("Could not extract preview frame")
    raw = result.stdout
    start = raw.find(b"\xff\xd8")
    end = raw.find(b"\xff\xd9", start if start != -1 else 0)
    if start == -1 or end == -1:
        raise RuntimeError("Could not decode preview frame")
    img = Image.open(io.BytesIO(bytes(raw[start:end + 2])))
    return img.convert("RGB")


# ---------------------------------------------------------------------------
# Helpers for retention transcript remap
# ---------------------------------------------------------------------------

def _chunk_words_to_segments(words: list[dict], max_gap: float = 0.8,
                             max_words: int = 40) -> list[dict]:
    """Rebuild caption-friendly segments from a flat remapped word list."""
    segments: list[dict] = []
    cur: list[dict] = []
    for w in words:
        if cur and (float(w.get("start", 0.0)) - float(cur[-1].get("end", 0.0)) > max_gap
                    or len(cur) >= max_words):
            segments.append(_pack_words(cur))
            cur = []
        cur.append(w)
    if cur:
        segments.append(_pack_words(cur))
    return segments


def _pack_words(words: list[dict]) -> dict:
    return {
        "start": float(words[0].get("start", 0.0)),
        "end": float(words[-1].get("end", 0.0)),
        "text": " ".join(str(w.get("word", "")) for w in words).strip(),
        "words": words,
    }

model_path = os.path.join(os.path.dirname(__file__), 'blaze_face_short_range.tflite')

def _ensure_model_exists():
  if not os.path.exists(model_path):
    import urllib.request
    url = "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
    urllib.request.urlretrieve(url, model_path)

_ensure_model_exists()
base_options = python.BaseOptions(model_asset_path=model_path)
options = vision.FaceDetectorOptions(base_options=base_options)
face_detector = vision.FaceDetector.create_from_options(options)


def _verify_output(path: str, expect_w: int, expect_h: int,
                   expect_dur: float, start: float) -> None:
  """ffprobe-based output check: streams + dims + duration. Raises on failure."""
  if not os.path.isfile(path) or os.path.getsize(path) < 10 * 1024:
    raise RuntimeError(
      f"Reframe produced no usable output for {start:.1f}-{start + expect_dur:.1f}s")
  try:
    r = subprocess.run(
      [resolve_ffprobe(), "-v", "error", "-show_entries",
       "stream=width,height,codec_type", "-show_entries", "format=duration",
       "-of", "json", path],
      capture_output=True, text=True, timeout=30)
    data = json.loads(r.stdout or "{}")
    vids = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    if not vids:
      raise RuntimeError("no video stream")
    w, h = int(vids[0].get("width", 0)), int(vids[0].get("height", 0))
    if (w, h) != (expect_w, expect_h):
      raise RuntimeError(f"bad dims {w}x{h}, want {expect_w}x{expect_h}")
    dur = float((data.get("format") or {}).get("duration", expect_dur))
    if abs(dur - expect_dur) > max(0.6, expect_dur * 0.15):
      raise RuntimeError(f"bad duration {dur:.2f}s, want {expect_dur:.2f}s")
  except RuntimeError:
    raise
  except Exception as exc:
    raise RuntimeError(f"Output verify failed: {exc}") from exc


async def process_clip(
  video_path: str,
  suggestion: dict[str, Any],
  output_dir: str,
  project_id: str,
  forced_layout_mode: Optional[str] = None,
  options: Optional[dict[str, Any]] = None,
  transcript: Optional[list[dict]] = None,
  progress_cb: Optional[Callable] = None,
  cancel_event: Optional[threading.Event] = None,
) -> dict[str, Any]:
  """
  Full pipeline for one clip (v2 modern edit):
  1. Cut raw clip at suggestion timestamps
  2. Trim leading dead air so the hook hits instantly (offsets start/end)
  3. Sample frames + detect faces
  4. Layout-aware reframe: face tracking with smoothing + punch-in zooms
  5. Loudness-normalized audio + subtle color punch, 1080x1920 output
  6. Return clip metadata (with ACTUAL start/end after hook trim)

  options: layout_mode, zoom_boost (0.8-1.6), x_offset (-1..1),
           static (bool, fixed crop), no_hook_trim (bool), grade (bool)
  """

  opts: dict[str, Any] = dict(options or {})
  if forced_layout_mode:
    opts["layout_mode"] = forced_layout_mode

  def _tick(stage: str, pct: float, msg: str = "") -> None:
    if cancel_event is not None and cancel_event.is_set():
      raise RuntimeError("Render cancelled by user")
    if progress_cb:
      progress_cb(stage, pct, msg)

  start = float(suggestion['start'])
  end = float(suggestion['end'])

  raw_clip = os.path.join(
    output_dir,
    f"raw_{project_id}_{suggestion['start']}.mp4"
  )
  _tick("cut", 8, "Cutting source range…")
  await _cut_raw_clip(video_path, start, end, raw_clip)

  width, height = _get_video_dimensions(raw_clip)
  duration = end - start

  # Hook trim — cut dead air at the head so the clip opens on the action.
  if not opts.get("no_hook_trim"):
    trimmed = _trim_leading_silence(raw_clip, duration)
    if trimmed > 0:
      start += trimmed
      duration -= trimmed
  _tick("trim", 18, "Nudging hook / trim")

  # Face-align opening — never open on an empty room. If no face appears in
  # the first seconds but one shows up shortly after, start the clip there
  # (viewers decide in 1-3s; shelves don't hook anyone). Runs BEFORE the
  # retention edit so word timings stay consistent.
  if not opts.get("no_face_align") and duration > 20:
    _fa_frames = _extract_sample_frames(raw_clip)
    _fa_faces = _detect_faces_all_frames(_fa_frames, width, height)
    first_face = next((i for i, sec in enumerate(_fa_faces) if sec), None)
    if first_face is not None and 2 < first_face <= 8 and duration - first_face >= 15.0:
      shift = float(first_face) - 0.5
      try:
        aligned_path = raw_clip + ".aligned.mp4"
        result = subprocess.run(
          [resolve_ffmpeg(), "-y", "-ss", f"{shift:.3f}", "-i", raw_clip,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-avoid_negative_ts", "make_zero", aligned_path],
          capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and os.path.exists(aligned_path):
          os.replace(aligned_path, raw_clip)
          start += shift
          duration -= shift
          logger.info("Face-align: opened %.1fs later at first face", shift)
      except Exception as exc:
        logger.warning("Face-align failed: %s", exc)

  # Retention edit — squeeze pauses + drop fillers, remap word timings.
  # Runs on the raw cut BEFORE tracking/captions so everything downstream
  # follows the edited timeline.
  edited_transcript: Optional[list[dict]] = None
  caption_window = (start, start + duration)
  cuts_applied = 0
  if transcript and not opts.get("no_retention"):
    try:
      abs_words = [w for seg in transcript for w in (seg.get("words") or [])
                   if start - 0.5 <= float(w.get("start", 0.0)) < start + duration + 0.5]
      if abs_words:
        rel_words = [dict(w, start=float(w["start"]) - start, end=float(w["end"]) - start)
                     for w in abs_words]
        silences = await asyncio.to_thread(
          _retention_mod.detect_silences, raw_clip, 0.0, duration)
        cuts = _retention_mod.plan_retention_cuts(rel_words, silences, 0.0, duration)
        if cuts:
          edited_path = raw_clip + ".edited.mp4"
          _retention_mod.render_edited_clip(raw_clip, cuts, edited_path, duration)
          os.replace(edited_path, raw_clip)
          removed = sum(c["end"] - c["start"] for c in cuts)
          duration -= removed
          cuts_applied = len(cuts)
          mapped = _editorial_mod.remap_words(rel_words, cuts)
          edited_transcript = _chunk_words_to_segments(mapped)
          caption_window = (0.0, duration)
          width, height = _get_video_dimensions(raw_clip)
          logger.info("Retention edit: %d cuts, -%.1fs, new duration %.1fs",
                      cuts_applied, removed, duration)
    except Exception as exc:
      logger.warning("Retention edit failed, using uncut raw: %s", exc)

  frames = _extract_sample_frames(raw_clip)
  face_data = _detect_faces_all_frames(frames, width, height)
  _tick("reframe", 30, "Detected faces — framing…")

  output_clip = os.path.join(
    output_dir,
    f"clip_{project_id}_{suggestion['start']}.mp4"
  )

  success, face_count, layout = await _reframe_tracked(
    raw_clip, output_clip, width, height, face_data, duration, frames, opts,
    progress_cb=lambda p, m: _tick("reframe", 30 + p * 0.5, m),
    cancel_event=cancel_event,
  )
  if not success:
    layout = await _reframe_static_v2(
      raw_clip, output_clip, width, height, face_data, frames, opts
    )
    face_counts = [len(f) for f in face_data]
    face_count = max(set(face_counts), key=face_counts.count) if face_counts else 0
  _tick("reframe", 82, "Reframe complete")

  if os.path.exists(raw_clip):
    os.remove(raw_clip)

  ar_w, ar_h, _ow, _oh = _aspect_spec(opts.get("aspect"))
  aspect_key = next(
    (k for k, (w, h, _, _) in ASPECTS.items() if (w, h) == (ar_w, ar_h)),
    DEFAULT_ASPECT,
  )

  # Face zone for caption placement — per-second (P4) + global fallback
  # Global zone (backwards compat) + per-second array for time-aware captions
  face_zones: list[Optional[str]] = []
  for sec in face_data:
    if not sec:
      face_zones.append(None)
    else:
      cy_vals = [f["cy"] for f in sec]
      median_cy = sorted(cy_vals)[len(cy_vals)//2] / max(float(height), 1.0)
      zone = "bottom" if median_cy > 0.62 else ("top" if median_cy < 0.35 else None)
      face_zones.append(zone)
  face_zone = None
  if face_count > 0:
    # Global fallback: majority zone
    from collections import Counter
    cnt = Counter([z for z in face_zones if z])
    if cnt:
      face_zone = cnt.most_common(1)[0][0]

  # Output guard: validate with ffprobe (not raw byte size — solid-color
  # test clips are tiny but valid). Checks streams, dims, duration.
  _verify_output(output_clip, _ow, _oh, duration, start)

  return {
    "file_path": output_clip,
    "start_time": start,
    "end_time": start + duration,
    "duration": duration,
    "title": suggestion.get('title', ''),
    "reason": suggestion.get('reason', ''),
    "viral_score": suggestion.get('viral_score', 0),
    "face_count": face_count,
    "layout_mode": layout,
    "aspect": aspect_key,
    "face_zone": face_zone,
    "face_zones": face_zones,
    "cuts_applied": cuts_applied,
    "transcript": edited_transcript,
    "caption_window": caption_window,
    "needs_user_confirm": False,
    "reframed": True
  }


async def plan_clip(
  video_path: str,
  suggestion: dict,
  output_mode: str = "auto",
  work_dir: Optional[str] = None,
) -> dict[str, Any]:
  """Cheap pre-analysis to choose crop vs blur-fill (+ caption style) for a clip.

  Cuts a throwaway copy of the clip range, samples frames, detects faces, then
  delegates to ``auto_output.resolve_plan``. No encode happens here.
  """
  start = float(suggestion["start"])
  end = float(suggestion["end"])
  wd = work_dir or os.path.dirname(os.path.abspath(video_path))
  probe = os.path.join(wd, f".plan_{abs(hash((video_path, start, end)))}.mp4")
  try:
    await _cut_raw_clip(video_path, start, end, probe)
    width, height = _get_video_dimensions(probe)
    frames = _extract_sample_frames(probe)
    faces = _detect_faces_all_frames(frames, width, height)
    return _auto_output_mod.resolve_plan(output_mode, width, height, frames, faces)
  except Exception as exc:
    logger.warning("plan_clip failed, defaulting to blur-fill: %s", exc)
    return {
      "strategy": "blur_fill",
      "aspect": "9:16",
      "canvas": "9:16",
      "caption_style": "minimal_clean",
      "layout_mode": "none",
      "has_screen_region": False,
      "avg_face_size": 0.0,
      "reason": "analysis_failed",
      "output_mode": output_mode,
    }
  finally:
    try:
      if os.path.exists(probe):
        os.remove(probe)
    except OSError:
      pass


def _extract_sample_frames(video_path: str) -> list[Image.Image]:
  """
  Extract 1 frame per second as raw bytes using FFmpeg.
  No temp files — pipe directly to memory.
  """
  ffmpeg_path = resolve_ffmpeg()
  try:
    result = subprocess.run([
      ffmpeg_path, "-i", video_path,
      "-vf", "fps=1",
      "-f", "image2pipe",
      "-vcodec", "mjpeg",
      "pipe:1"
    ], capture_output=True)
  except FileNotFoundError as e:
    raise RuntimeError(
      f"ffmpeg not found at '{ffmpeg_path}'. Install FFmpeg "
      f"(winget install Gyan.FFmpeg) and restart the backend."
    ) from e

  raw = result.stdout
  frames = []
  start = 0
  while True:
    start = raw.find(b'\xff\xd8', start)
    if start == -1:
      break
    end = raw.find(b'\xff\xd9', start)
    if end == -1:
      break
    jpeg_bytes = bytes(raw[start:end + 2])
    try:
      img = Image.open(io.BytesIO(jpeg_bytes))
      frames.append(img)
    except Exception:
      pass
    start = end + 2

  return frames


def _detect_faces_all_frames(
  frames: list[Image.Image],
  vid_width: int,
  vid_height: int
) -> list[list[dict]]:
  """
  Run MediaPipe face detection on each PIL frame.
  Applies deduplication tracking rules.
  """
  import numpy as np  # type: ignore

  all_faces = []
  for frame in frames:
    rgb = np.array(frame.convert('RGB'))
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    results = face_detector.detect(mp_image)
    faces = []
    if results.detections:
      for det in results.detections:
        bb = det.bounding_box
        cx = bb.origin_x + bb.width / 2
        cy = bb.origin_y + bb.height / 2
        fw = bb.width
        fh = bb.height
        size = bb.height / vid_height

        # Deduplication Rule: Remove size < 0.05
        if size < 0.05:
            continue

        faces.append({
          "cx": cx, "cy": cy,
          "w": fw, "h": fh,
          "size": size
        })

    # Deduplication Rule: Remove detections within 50px of another
    valid_faces = []
    for f in faces:
        is_dup = False
        for vf in valid_faces:
            if math.hypot(f['cx'] - vf['cx'], f['cy'] - vf['cy']) < 50:
                is_dup = True
                break
        if not is_dup:
            valid_faces.append(f)

    all_faces.append(valid_faces)

  # Persistence filter: drop flicker false-positives (UI icons, avatars).
  # Keep small-but-real close-up faces: size threshold is 0.04 for
  # single-face frames (common for phone-held talking head), 0.06 for
  # multi-face to avoid UI-icon ghosts.
  if all_faces:
    persist_radius = max(60.0, float(vid_width) * 0.06)
    filtered: list[list[dict]] = []
    for i, sec_faces in enumerate(all_faces):
      min_size = 0.04 if len(sec_faces) == 1 else 0.06
      keep = []
      for f in sec_faces:
        if f.get("size", 0) < min_size:
          continue
        neighbours = []
        if i > 0:
          neighbours += all_faces[i - 1]
        if i + 1 < len(all_faces):
          neighbours += all_faces[i + 1]
        if not neighbours:
          keep.append(f)
          continue
        if any(math.hypot(f["cx"] - g["cx"], f["cy"] - g["cy"]) < persist_radius
               for g in neighbours if g.get("size", 0) >= 0.04):
          keep.append(f)
        elif len(sec_faces) == 1 and f.get("size", 0) >= 0.06:
          # Single large face with no neighbour — likely real (brief detection gap)
          keep.append(f)
      filtered.append(keep)
    all_faces = filtered
  return all_faces


def _ease_in_out(t: float) -> float:
    return 2 * t * t if t < 0.5 else 1 - math.pow(-2 * t + 2, 2) / 2


def _tracking_targets(face_data: list[list[dict]], frames, duration: float,
                      vid_width: int, vid_height: int,
                      options: dict, input_path: str = "") -> tuple[list[dict], int, str, int]:
  """Per-second raw camera targets with gap-fill + shot-aware EMA smoothing.

  Shared by the reframe path and the Social Canvas foreground (single source
  of truth for face tracking; renderers only differ in how targets become
  pixels). Returns (raw, face_count, layout, total_secs) where each raw entry
  is {zoom, cx, cy, face, cut, manual?}.
  """
  total_secs = max(1, math.ceil(duration))
  cx_all = vid_width / 2.0
  cy_all = vid_height / 2.0

  face_counts = [len(f) for f in face_data]
  face_count = max(set(face_counts), key=face_counts.count) if face_counts else 0
  layout = _resolve_layout(frames, face_data, vid_width, vid_height, face_count, options)

  tracking = bool(options.get("tracking", True)) and not bool(options.get("static", False))

  # Detect scene cuts for shot-aware smoothing (P3)
  scene_cuts: set[int] = set()
  try:
    # Use signals module if available (fast, limited to duration)
    cuts = _signals_mod._scene_cuts(input_path, duration) if hasattr(_signals_mod, "_scene_cuts") else []
    scene_cuts = {int(c) for c in cuts}
  except Exception:
    scene_cuts = set()

  # Per-second raw targets (1 sample/sec from face_data).
  # MANUAL keyframes override AUTO for their time region
  manual_kfs = options.get("crop_keyframes") if options.get("crop_mode") == "manual" else None
  if manual_kfs:
    manual_kfs = _validate_keyframes(manual_kfs, duration)

  raw: list[dict] = []
  for idx in range(total_secs):
    t = float(idx)  # segment start time
    # If manual keyframes cover this time, use interpolated manual crop
    if manual_kfs:
      x_norm, y_norm, zoom_m = _interpolate_keyframes(manual_kfs, t, duration)
      # Convert normalized x/y to source cx/cy
      cx_m = x_norm * vid_width
      cy_m = y_norm * vid_height
      # Manual zoom is absolute, not face-based
      raw.append({"zoom": zoom_m, "cx": cx_m, "cy": cy_m, "face": False, "cut": False, "manual": True})
      continue

    sec = face_data[idx] if idx < len(face_data) else []
    # Scene cut at this second → will reset EMA later
    is_cut = idx in scene_cuts

    if not tracking or layout == "none" or not sec:
      raw.append({"zoom": 1.0, "cx": cx_all, "cy": cy_all, "face": False, "cut": is_cut})
    elif layout in ("podcast", "panel"):
      cx = sum(f["cx"] for f in sec) / len(sec)
      cy = sum(f["cy"] for f in sec) / len(sec)
      zoom = min(1.5, _zoom_for_size(max(f["size"] for f in sec)) * 0.8)
      raw.append({"zoom": zoom, "cx": cx, "cy": cy, "face": True, "cut": is_cut})
    elif layout == "tutorial":
      # P2: Content-aware tutorial framing — blend face + screen content
      face = max(sec, key=lambda f: f['size'])
      frame = frames[idx] if idx < len(frames) else None
      if frame is not None:
        content_cx, content_cy, area_ratio = _estimate_content_center(frame, sec, vid_width, vid_height)
        # If content dominates (large screen, small face), weight toward content
        face_size = face['size']
        if area_ratio > 0.35 and face_size < 0.08:
          w_face, w_content = 0.3, 0.7
          zoom = min(_zoom_for_size(face_size), 1.6)  # wider to include screen
        elif area_ratio > 0.25:
          w_face, w_content = 0.5, 0.5
          zoom = min(_zoom_for_size(face_size), 1.8)
        else:
          w_face, w_content = 0.7, 0.3
          zoom = min(_zoom_for_size(face_size), 2.0)
        cx = face['cx'] * w_face + content_cx * w_content
        cy = face['cy'] * w_face + content_cy * w_content
      else:
        zoom = min(_zoom_for_size(face['size']), 2.0)
        cx, cy = face['cx'], face['cy']
      raw.append({"zoom": zoom, "cx": cx, "cy": cy, "face": True, "cut": is_cut})
    else:
      face = max(sec, key=lambda f: f['size'])
      zoom = _zoom_for_size(face['size'])
      raw.append({"zoom": zoom, "cx": face['cx'], "cy": face['cy'], "face": True, "cut": is_cut})

  # Fill face-loss gaps — skip for manual (explicit user control)
  if not manual_kfs:
    last_z, last_x, last_y = 1.0, cx_all, cy_all
    for r in raw:
      if r["face"]:
        last_z, last_x, last_y = r["zoom"], r["cx"], r["cy"]
      else:
        last_x += (cx_all - last_x) * 0.2
        last_y += (cy_all - last_y) * 0.2
        last_z += (1.0 - last_z) * 0.1
        r["zoom"], r["cx"], r["cy"] = last_z, last_x, last_y

    # EMA smoothing — shot-aware (P3): reset on scene cuts, skip for manual
    ALPHA = 0.35
    sm_z, sm_x, sm_y = raw[0]["zoom"], raw[0]["cx"], raw[0]["cy"]
    for r in raw:
      if r.get("cut"):
        # Hard cut: snap to new shot's target, no smoothing across cut
        sm_z, sm_x, sm_y = r["zoom"], r["cx"], r["cy"]
      else:
        sm_z += (r["zoom"] - sm_z) * ALPHA
        sm_x += (r["cx"] - sm_x) * ALPHA
        sm_y += (r["cy"] - sm_y) * ALPHA
      r["zoom"], r["cx"], r["cy"] = sm_z, sm_x, sm_y

  return raw, face_count, layout, total_secs


async def _reframe_tracked(
  input_path: str,
  output_path: str,
  vid_width: int,
  vid_height: int,
  face_data: list[list[dict]],
  duration: float,
  frames,
  options: dict,
  progress_cb: Optional[Callable] = None,
  cancel_event: Optional[threading.Event] = None,
) -> tuple[bool, int, str]:
  """
  Layout-aware tracked reframe with EMA smoothing + punch-in zooms.
  Segmented render: one STATIC crop per 1-second chunk, concatenated.
  """
  raw, face_count, layout, total_secs = _tracking_targets(
      face_data, frames, duration, vid_width, vid_height, options,
      input_path=input_path)

  ar_w, ar_h, out_w, out_h = _aspect_spec(options.get("aspect"))
  zoom_boost = _clamp(float(options.get("zoom_boost", 1.0)), 0.8, 1.6)
  x_offset = _clamp(float(options.get("x_offset", 0.0)), -1.0, 1.0)
  y_offset = _clamp(float(options.get("y_offset", 0.0)), -1.0, 1.0)
  tracking = bool(options.get("tracking", True)) and not bool(options.get("static", False))
  use_punch = tracking and layout not in ("none",)

  # Segmented render: one STATIC crop per 1-second chunk, concatenated.
  # Uses a single loudnorm pass on the final file to avoid double-lossy.
  grade = _grade_suffix(options)
  ffmpeg_path = resolve_ffmpeg()
  work_dir = os.path.dirname(os.path.abspath(output_path))
  tag = os.path.splitext(os.path.basename(output_path))[0]
  chunk_files: list[str] = []
  lst_file = os.path.join(work_dir, f".concat_{tag}.txt")
  joined_path = output_path + ".joined.mp4"

  def _cleanup(paths: list[str]) -> None:
    for p in paths:
      try:
        if os.path.exists(p):
          os.remove(p)
      except OSError:
        pass

  try:
    for s_idx in range(total_secs):
      if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Render cancelled by user")
      if progress_cb:
        progress_cb(max(0.0, min(0.95, float(s_idx) / max(total_secs, 1))),
                    f"Reframing segment {s_idx + 1}/{total_secs}")
      seg_dur = min(1.0, duration - s_idx * 1.0)
      if seg_dur <= 0.05:
        break
      r = raw[s_idx]
      zoom = _clamp(r["zoom"] * zoom_boost, 0.5, 3.0)
      if use_punch and not r.get("manual"):
        zoom = _clamp(zoom * _punch_factor(float(s_idx)), 0.5, 2.4)
      # Quality cap: keep the crop large enough that it never upscales past
      # MAX_UPSCALE on the way to the output canvas.
      if not r.get("manual"):
        zoom = min(zoom, _upscale_cap_zoom(vid_height, out_h))
      crop_w, crop_h, crop_x, crop_y = _compute_crop(
        vid_width, vid_height, zoom, r["cx"], r["cy"],
        ar_w, ar_h, x_offset, y_offset)
      # Upscale guard: never upscale more than ~2x (mushy phones). Expand
      # the crop back out when zoom would make it tiny.
      if crop_w * 2 < out_w or crop_h * 2 < out_h:
        need_w, need_h = (out_w + 1) // 2, (out_h + 1) // 2
        # keep aspect: grow to cover need_w x need_h
        grow = max(need_w / max(crop_w, 1), need_h / max(crop_h, 1))
        gw, gh = min(int(vid_width), int(round(crop_w * grow))) & ~1, min(int(vid_height), int(round(crop_h * grow))) & ~1
        gw, gh = max(2, gw), max(2, gh)
        sx, sy = float(vid_width) - gw, float(vid_height) - gh
        crop_x = int(_clamp(r["cx"] - gw / 2.0, 0.0, max(sx, 0.0))) & ~1
        crop_y = int(_clamp(r["cy"] - gh / 2.0, 0.0, max(sy, 0.0))) & ~1
        crop_w, crop_h = gw, gh
        logger.info("Upscale guard: zoom %.2f capped, crop %dx%d", zoom, crop_w, crop_h)
      filt = (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={out_w}:{out_h}:flags=lanczos:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{grade}"
      )
      chunk = os.path.join(work_dir, f".seg{s_idx:04d}_{tag}.mp4")
      result = subprocess.run(
        [ffmpeg_path, "-y", "-ss", f"{s_idx:.3f}", "-i", input_path,
         "-t", f"{seg_dur:.3f}", "-vf", filt,
         "-c:v", "libx264", "-crf", "17", "-preset", "medium",
         "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
         "-fps_mode", "cfr", "-pix_fmt", "yuv420p",
         "-avoid_negative_ts", "make_zero", "-an", chunk],
        capture_output=True, timeout=120)
      if result.returncode != 0:
        logger.error("Segment %d failed: %s", s_idx,
                     result.stderr.decode(errors="replace")[-500:])
        raise RuntimeError(f"Segment {s_idx} reframe failed")
      chunk_files.append(chunk)

    if not chunk_files:
      return False, face_count, layout

    with open(lst_file, "w") as f:
      for p in chunk_files:
        f.write(f"file '{p}'\n")
    result = subprocess.run(
      [ffmpeg_path, "-y", "-fflags", "+genpts",
       "-f", "concat", "-safe", "0",
       "-i", lst_file, "-c", "copy",
       "-avoid_negative_ts", "make_zero", joined_path],
      capture_output=True, timeout=120)
    if result.returncode != 0:
      logger.error("Segment join failed: %s", result.stderr.decode(errors="replace")[-500:])
      raise RuntimeError("Segment join failed")

    # Single audio pass: extract from original + loudnorm, mux with video.
    # Always emit AAC stereo 48k so phones never get a silent/corrupt track.
    if _has_audio(input_path):
      result = subprocess.run(
        [ffmpeg_path, "-y", "-i", joined_path, "-i", input_path,
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy",
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-shortest", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", output_path],
        capture_output=True, timeout=300)
    else:
      result = subprocess.run(
        [ffmpeg_path, "-y", "-i", joined_path,
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", output_path],
        capture_output=True, timeout=120)
    if result.returncode != 0:
      logger.error("Finalize failed: %s", result.stderr.decode(errors="replace")[-500:])
      raise RuntimeError("Finalize failed")
  except FileNotFoundError as e:
    raise RuntimeError(
      f"ffmpeg not found at '{ffmpeg_path}'. Install FFmpeg "
      f"(winget install Gyan.FFmpeg) and restart the backend."
    ) from e
  except subprocess.TimeoutExpired as e:
    _cleanup(chunk_files + [lst_file, joined_path])
    raise RuntimeError(f"Reframe timed out: {e}") from e
  except Exception:
    _cleanup(chunk_files + [lst_file, joined_path])
    return False, face_count, layout

  _cleanup(chunk_files + [lst_file, joined_path])
  return True, face_count, layout


async def _reframe_static_v2(
  input_path: str,
  output_path: str,
  vid_width: int,
  vid_height: int,
  face_data: list[list[dict]],
  frames,
  options: dict,
) -> str:
  """Fixed-crop fallback with loud audio + grade. Returns layout_mode."""
  all_faces = [f for sec_data in face_data for f in sec_data]
  avg_cx = (sum(f["cx"] for f in all_faces) / len(all_faces)) if all_faces else vid_width / 2.0
  avg_cy = (sum(f["cy"] for f in all_faces) / len(all_faces)) if all_faces else vid_height / 2.0

  ar_w, ar_h, out_w, out_h = _aspect_spec(options.get("aspect"))
  zoom_boost = _clamp(float(options.get("zoom_boost", 1.0)), 0.8, 1.6)
  x_offset = _clamp(float(options.get("x_offset", 0.0)), -1.0, 1.0)
  y_offset = _clamp(float(options.get("y_offset", 0.0)), -1.0, 1.0)

  crop_w, crop_h, crop_x, crop_y = _compute_crop(
    vid_width, vid_height,
    min(zoom_boost, _upscale_cap_zoom(vid_height, out_h)), avg_cx, avg_cy,
    ar_w, ar_h, x_offset, y_offset)

  grade = _grade_suffix(options)
  filter_cx = (
    f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
    f"scale={out_w}:{out_h}:flags=lanczos:force_original_aspect_ratio=decrease,"
    f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{grade}"
  )
  ffmpeg_path = resolve_ffmpeg()
  cmd = [ffmpeg_path, "-i", input_path, "-vf", filter_cx,
         *_audio_args(input_path), *_encode_args(), output_path, "-y"]
  try:
    result = subprocess.run(cmd, capture_output=True)
  except FileNotFoundError as e:
    raise RuntimeError(
      f"ffmpeg not found at '{ffmpeg_path}'. Install FFmpeg "
      f"(winget install Gyan.FFmpeg) and restart the backend."
    ) from e
  if result.returncode != 0:
    raise RuntimeError(f"FFmpeg static reframe failed: {result.stderr.decode()[:500]}")

  face_counts = [len(f) for f in face_data]
  face_count = max(set(face_counts), key=face_counts.count) if face_counts else 0
  return _resolve_layout(frames, face_data, vid_width, vid_height, face_count, options)


# ---------------------------------------------------------------------------
# Social Canvas render (MODE B): blurred duplicate background + foreground.
# ---------------------------------------------------------------------------

def _social_fg_targets(raw_entry: dict, manual_kfs, t: float, duration: float,
                       vid_width: int, vid_height: int) -> tuple[float, float, float]:
  """Foreground content center/zoom for one segment.

  fit=reframe (or any manual keyframes): keyframe/tracked zoom window.
  Returns (cx, cy, zoom) in source pixels + absolute zoom.
  """
  if manual_kfs:
    x_n, y_n, z_m = _interpolate_keyframes(manual_kfs, t, duration)
    return x_n * vid_width, y_n * vid_height, z_m
  return raw_entry["cx"], raw_entry["cy"], raw_entry["zoom"]


def _social_segment_filter(seg: dict) -> str:
  """Build the single-graph social filter for one segment.

  seg keys: sw, sh (source), cw, ch (canvas), bg{...}, fg{...}, box{...},
  overlay_xy, grade, progress, logo, seg_off, seg_total.
  Inputs: [0:v] source (+ [1:v] logo when seg['logo'] set).
  Output label: [vout].
  """
  sw, sh, cw, ch = seg["sw"], seg["sh"], seg["cw"], seg["ch"]
  bg, fg, box = seg["bg"], seg["fg"], seg["box"]
  ox, oy = seg["overlay_xy"]
  parts: list[str] = []

  # ---- background branch (same source, same time) ----
  if bg["mode"] == "solid_color":
    parts.append(
      f"color=c={bg['solid']}:s={cw}x{ch}:r=30:d={seg['dur']:.3f},"
      f"format=yuv420p,setsar=1[bg]")
  else:
    cb = bg["cover"]
    parts.append(
      f"[0:v]scale={cb['scaled_w']}:{cb['scaled_h']}:flags=lanczos,"
      f"setsar=1,crop={cb['crop_w']}:{cb['crop_h']}:{cb['crop_x']}:{cb['crop_y']}[bg0]")
    mods: list[str] = []
    if bg.get("blur", 0.0) > 0:
      mods.append(f"gblur=sigma={bg['blur']:.1f}")
    mods.append(f"eq=brightness={bg['bright']:.3f}:contrast={bg['contrast']:.3f}")
    if bg.get("tint"):
      mods.append(f"drawbox=x=0:y=0:w=iw:h=ih:c={bg['tint']}:t=fill")
    parts.append("[bg0]" + ",".join(mods) + "[bg1]")
    if bg.get("opacity", 1.0) < 1.0:
      parts.append(
        f"color=c=black:s={cw}x{ch}:r=30:d={seg['dur']:.3f},format=yuv420p[base];"
        f"[bg1]format=rgba,colorchannelmixer=aa={bg['opacity']:.2f}[bgA];"
        f"[base][bgA]overlay=0:0:format=auto[bg]")
    else:
      parts.append("[bg1]copy[bg]")

  # ---- foreground branch ----
  if fg.get("crop"):
    c = fg["crop"]
    parts.append(
      f"[0:v]crop={c[0]}:{c[1]}:{c[2]}:{c[3]},"
      f"scale={box['box_w']}:{box['box_h']}:flags=lanczos,setsar=1[fg0]")
  else:
    parts.append(
      f"[0:v]scale={box['box_w']}:{box['box_h']}:flags=lanczos,setsar=1[fg0]")
  chain, need_shadow, sigma = _composition_mod.fg_chain(
      box["box_w"], box["box_h"], fg.get("radius", 0),
      fg.get("shadow", "none"), fg.get("border") or {}, seg.get("grade", ""))
  parts.append(f"[fg0]{chain}[fg1]")
  if need_shadow:
    schain, dx, dy = _composition_mod.shadow_chain(sigma)
    parts.append(f"[fg1]split=2[fgm][fgs];[fgs]{schain}[sh];"
                 f"[bg][sh]overlay={ox + dx}:{oy + dy}:format=auto[t1];"
                 f"[t1][fgm]overlay={ox}:{oy}:format=auto[ov]")
  else:
    parts.append(f"[bg][fg1]overlay={ox}:{oy}:format=auto[ov]")

  cur = "[ov]"
  # ---- optional gradient veil ----
  if seg.get("gradient"):
    g = seg["gradient"]
    rt, gt, bt = g["top_rgb"]
    rb, gb, bb = g["bottom_rgb"]
    parts.append(
      f"nullsrc=s={cw}x{ch}:r=30:d={seg['dur']:.3f},format=rgba,"
      f"geq=r='{rt}+({rb}-{rt})*Y/{ch}':g='{gt}+({gb}-{gt})*Y/{ch}':"
      f"b='{bt}+({bb}-{bt})*Y/{ch}',"
      f"colorchannelmixer=aa={g['opacity']:.2f}[gr];"
      f"{cur}[gr]overlay=0:0:format=auto[ovg]")
    cur = "[ovg]"
  # ---- progress bar (real rendered time) ----
  if seg.get("progress"):
    p = seg["progress"]
    y = 0 if p["position"] == "top" else ch - p["height"]
    parts.append(
      f"{cur}drawbox=x=0:y={y}:w='(t+{seg['off']:.3f})*{cw}/{seg['total']:.3f}':"
      f"h={p['height']}:c={p['color']}:t=fill[vp]")
    cur = "[vp]"
  # ---- image logo watermark ----
  if seg.get("logo"):
    lg = seg["logo"]
    parts.append(
      f"[1:v]scale={lg['w']}:{lg['h']}:flags=lanczos,format=rgba,"
      f"colorchannelmixer=aa={lg['opacity']:.2f}[logo];"
      f"{cur}[logo]overlay={lg['x']}:{lg['y']}:format=auto[vout]")
  else:
    parts.append(f"{cur}copy[vout]")
  return ";".join(parts)


async def render_social(video_path: str,
                        suggestion: dict,
                        output_dir: str,
                        project_id: str,
                        options: Optional[dict] = None,
                        transcript: Optional[list[dict]] = None,
                        progress_cb: Optional[Callable] = None,
                        cancel_event: Optional[threading.Event] = None) -> dict:
  """MODE B render: 16:9 source -> 9:16 Social Canvas.

  Same-source blurred background (synchronized per segment) + foreground
  (fit or reframe window, own scale/pos/radius/shadow/border) + gradient,
  progress, logo overlays + single loudnorm audio mux + canvas captions.

  options: composition (normalized state), canvas ("9:16"...), crop_mode,
  crop_keyframes, tracking, zoom_boost, grade, no_retention.
  Returns the same metadata shape as process_clip.
  """
  comp_mod = _composition_mod
  opts: dict = dict(options or {})
  comp = comp_mod.normalize_composition(opts.get("composition"),
                                        opts.get("canvas") or opts.get("aspect") or "9:16")
  cw, ch = comp_mod.canvas_size(comp["canvas"])
  start = float(suggestion["start"])
  end = float(suggestion["end"])

  def _tick(stage: str, pct: float, msg: str = "") -> None:
    if cancel_event is not None and cancel_event.is_set():
      raise RuntimeError("Render cancelled by user")
    if progress_cb:
      progress_cb(stage, pct, msg)

  raw_clip = os.path.join(output_dir, f"raw_{project_id}_{suggestion['start']}.mp4")
  _tick("cut", 6, "Cutting source range…")
  await _cut_raw_clip(video_path, start, end, raw_clip)
  width, height = _get_video_dimensions(raw_clip)
  duration = end - start

  # Retention edit (same modules as reframe path, before tracking/captions)
  edited_transcript: Optional[list[dict]] = None
  caption_window = (start, start + duration)
  cuts_applied = 0
  _tick("trim", 14, "Trimming dead air…")
  if transcript and not opts.get("no_retention"):
    try:
      abs_words = [w for seg in transcript for w in (seg.get("words") or [])
                   if start - 0.5 <= float(w.get("start", 0.0)) < start + duration + 0.5]
      if abs_words:
        rel_words = [dict(w, start=float(w["start"]) - start, end=float(w["end"]) - start)
                     for w in abs_words]
        silences = await asyncio.to_thread(
          _retention_mod.detect_silences, raw_clip, 0.0, duration)
        cuts = _retention_mod.plan_retention_cuts(rel_words, silences, 0.0, duration)
        if cuts:
          edited_path = raw_clip + ".edited.mp4"
          _retention_mod.render_edited_clip(raw_clip, cuts, edited_path, duration)
          os.replace(edited_path, raw_clip)
          removed = sum(c["end"] - c["start"] for c in cuts)
          duration -= removed
          cuts_applied = len(cuts)
          mapped = _editorial_mod.remap_words(rel_words, cuts)
          edited_transcript = _chunk_words_to_segments(mapped)
          caption_window = (0.0, duration)
          width, height = _get_video_dimensions(raw_clip)
    except Exception as exc:
      logger.warning("Social retention edit failed, using uncut raw: %s", exc)

  frames = _extract_sample_frames(raw_clip)
  face_data = _detect_faces_all_frames(frames, width, height)
  face_counts = [len(f) for f in face_data]
  face_count = max(set(face_counts), key=face_counts.count) if face_counts else 0
  _tick("reframe", 25, "Detected faces — composing…")

  fg_opts = dict(opts)
  raw, _fc, layout, total_secs = _tracking_targets(
      face_data, frames, duration, width, height, fg_opts, input_path=raw_clip)
  face_count = _fc or face_count

  bg_cfg, fg_cfg = comp["background"], comp["foreground"]
  zoom_boost = _clamp(float(opts.get("zoom_boost", 1.0)), 0.8, 1.6)
  grade = _grade_suffix(opts)
  manual_kfs = _validate_keyframes(opts.get("crop_keyframes"), duration) \
      if opts.get("crop_mode") == "manual" and opts.get("crop_keyframes") else None
  # Bg motion presets reuse the standard keyframe interpolation (no new system)
  bg_motion_kfs = comp_mod.bg_motion_keyframes(bg_cfg.get("motion", "none"), duration)
  use_reframe_fg = fg_cfg.get("fit") == "reframe" or bool(manual_kfs)

  def _hex_rgb(hx: str) -> tuple[int, int, int]:
    hx = str(hx or "#000000").lstrip("#")
    if len(hx) == 3:
      hx = "".join(c * 2 for c in hx)
    try:
      return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    except Exception:
      return 0, 0, 0

  output_clip = os.path.join(output_dir, f"clip_{project_id}_{suggestion['start']}.mp4")
  ffmpeg_path = resolve_ffmpeg()
  work_dir = os.path.dirname(os.path.abspath(output_clip))
  tag = os.path.splitext(os.path.basename(output_clip))[0]
  chunk_files: list[str] = []
  lst_file = os.path.join(work_dir, f".concat_{tag}.txt")
  joined_path = output_clip + ".joined.mp4"

  def _cleanup(paths: list[str]) -> None:
    for p in paths:
      try:
        if os.path.exists(p):
          os.remove(p)
      except OSError:
        pass

  # Logo watermark pre-scale (image kind only; text goes through ASS captions)
  logo_cfg = None
  wm = comp["overlays"]["watermark"]
  if wm.get("enabled") and wm.get("kind") == "image" and wm.get("image"):
    lp = os.path.normpath(os.path.join(work_dir, os.path.basename(str(wm["image"]))))
    if os.path.isfile(lp):
      lw = max(16, int(cw * 0.18 * float(wm.get("scale", 1.0))))
      lh = max(16, int(lw * 0.5))
      pos = str(wm.get("position", "top-right"))
      lx = cw - lw - 24 if "right" in pos else (24 if "left" in pos else (cw - lw) // 2)
      ly = 24 if "top" in pos else (ch - lh - 24 if "bottom" in pos else (ch - lh) // 2)
      logo_cfg = {"path": lp, "w": lw, "h": lh, "x": lx, "y": ly,
                  "opacity": float(wm.get("opacity", 0.6))}
    else:
      logger.warning("Social logo image not found: %s", wm.get("image"))

  try:
    for s_idx in range(total_secs):
      if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Render cancelled by user")
      if progress_cb:
        progress_cb("reframe", 25 + (float(s_idx) / max(total_secs, 1)) * 0.6,
                    f"Composing segment {s_idx + 1}/{total_secs}")
      seg_dur = min(1.0, duration - s_idx * 1.0)
      if seg_dur <= 0.05:
        break
      t = float(s_idx)
      r = raw[s_idx] if s_idx < len(raw) else raw[-1]

      # --- foreground content + box ---
      if use_reframe_fg:
        cx, cy, zm = _social_fg_targets(r, manual_kfs, t, duration, width, height)
        zoom = _clamp(zm * zoom_boost, 0.5, 3.0)
        fcr = _compute_crop(width, height, zoom, cx, cy, width, height, 0.0, 0.0)
        content = {"crop": fcr}
        src_cx_n, src_cy_n = cx / max(width, 1), cy / max(height, 1)
      else:
        content = {}
        src_cx_n = src_cy_n = 0.5
      # Box scale/pos: manual keyframes drive punch-in/out on the box
      box_scale, box_fx, box_fy = fg_cfg["scale"], fg_cfg["x"], fg_cfg["y"]
      if manual_kfs and not use_reframe_fg:
        kx, ky, kz = _interpolate_keyframes(manual_kfs, t, duration)
        box_scale = _clamp(fg_cfg["scale"] * kz, 0.5, 1.5)
        box_fx, box_fy = kx, ky
      box = comp_mod.fg_fit_box(width, height, cw, ch, box_scale, box_fx, box_fy)

      # --- background (same source, same time) ---
      if bg_cfg.get("follow_foreground"):
        bnx, bny = src_cx_n, src_cy_n
      elif bg_motion_kfs:
        bnx, bny, _bz = _interpolate_keyframes(bg_motion_kfs, t, duration)
        bg_cfg = dict(bg_cfg)  # motion zoom handled below via scale mult
      else:
        bnx, bny = bg_cfg["x"], bg_cfg["y"]
      bscale = bg_cfg["scale"]
      if bg_motion_kfs:
        _mx, _my, mz = _interpolate_keyframes(bg_motion_kfs, t, duration)
        bscale = _clamp(bscale * mz, 1.0, 1.5)
      cover = comp_mod.bg_cover_box(width, height, cw, ch, bscale, bnx, bny)
      blur_v = float(bg_cfg.get("blur_value", 18.0)) if bg_cfg.get("mode") == "blurred_video" else 0.0
      if bg_cfg.get("mode") == "darkened_video":
        blur_v = 0.0
      tint = None
      if bg_cfg.get("tint") == "black" or (bg_cfg.get("tint") == "custom" and bg_cfg.get("tint_opacity", 0) > 0):
        op = 0.35 if bg_cfg.get("tint") == "black" else float(bg_cfg.get("tint_opacity", 0.0))
        col = "#000000" if bg_cfg.get("tint") == "black" else bg_cfg.get("tint_color", "#000000")
        tint = f"{comp_mod._hex_to_ff(col)}@{op:.2f}"
      bg = {"mode": bg_cfg.get("mode", "blurred_video"),
            "solid": comp_mod._hex_to_ff(bg_cfg.get("solid_color", "#0a0a0f")),
            "cover": cover, "blur": blur_v,
            "bright": comp_mod.effective_brightness(bg_cfg),
            "contrast": float(bg_cfg.get("contrast", 1.0)),
            "opacity": float(bg_cfg.get("opacity", 1.0)), "tint": tint}

      fg = {"crop": content.get("crop"),
            "radius": int(fg_cfg.get("radius", 0)),
            "shadow": fg_cfg.get("shadow", "soft"),
            "border": fg_cfg.get("border") or {}}

      grad = None
      if bg_cfg.get("gradient"):
        grad = {"top_rgb": _hex_rgb(bg_cfg.get("gradient_top", "#000000")),
                "bottom_rgb": _hex_rgb(bg_cfg.get("gradient_bottom", "#000000")),
                "opacity": float(bg_cfg.get("gradient_opacity", 0.35))}
      prog = None
      if comp["overlays"]["progress"].get("enabled"):
        pr = comp["overlays"]["progress"]
        prog = {"position": pr.get("position", "bottom"),
                "color": comp_mod._hex_to_ff(pr.get("color", "#ffffff")),
                "height": int(pr.get("height", 8))}

      seg = {"sw": width, "sh": height, "cw": cw, "ch": ch, "bg": bg, "fg": fg,
             "box": box, "overlay_xy": (box["x"], box["y"]), "grade": grade,
             "gradient": grad, "progress": prog,
             "logo": dict(logo_cfg) if logo_cfg else None,
             "dur": seg_dur, "off": t, "total": duration}
      filt = _social_segment_filter(seg)
      chunk = os.path.join(work_dir, f".seg{s_idx:04d}_{tag}.mp4")
      cmd = [ffmpeg_path, "-y", "-ss", f"{s_idx:.3f}", "-i", raw_clip]
      if logo_cfg:
        cmd += ["-i", logo_cfg["path"]]
      cmd += ["-t", f"{seg_dur:.3f}", "-filter_complex", filt,
              "-map", "[vout]", "-c:v", "libx264", "-crf", "17",
              "-preset", "medium", "-g", "30", "-keyint_min", "30",
              "-sc_threshold", "0", "-fps_mode", "cfr", "-pix_fmt", "yuv420p",
              "-avoid_negative_ts", "make_zero", "-an", chunk]
      result = subprocess.run(cmd, capture_output=True, timeout=180)
      if result.returncode != 0:
        logger.error("Social segment %d failed: %s", s_idx,
                     result.stderr.decode(errors="replace")[-600:])
        raise RuntimeError(f"Social segment {s_idx} failed")
      chunk_files.append(chunk)

    if not chunk_files:
      raise RuntimeError("Social render produced no segments")

    with open(lst_file, "w") as f:
      for p in chunk_files:
        f.write(f"file '{p}'\n")
    result = subprocess.run(
      [ffmpeg_path, "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0",
       "-i", lst_file, "-c", "copy", "-avoid_negative_ts", "make_zero", joined_path],
      capture_output=True, timeout=180)
    if result.returncode != 0:
      logger.error("Social join failed: %s", result.stderr.decode(errors="replace")[-500:])
      raise RuntimeError("Social segment join failed")

    if _has_audio(raw_clip):
      result = subprocess.run(
        [ffmpeg_path, "-y", "-i", joined_path, "-i", raw_clip,
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-shortest", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", output_clip],
        capture_output=True, timeout=300)
    else:
      result = subprocess.run(
        [ffmpeg_path, "-y", "-i", joined_path,
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", output_clip],
        capture_output=True, timeout=180)
    if result.returncode != 0:
      logger.error("Social finalize failed: %s", result.stderr.decode(errors="replace")[-500:])
      raise RuntimeError("Social finalize failed")
  except Exception:
    _cleanup(chunk_files + [lst_file, joined_path])
    raise
  _cleanup(chunk_files + [lst_file, joined_path])

  if os.path.exists(raw_clip):
    try:
      os.remove(raw_clip)
    except OSError:
      pass

  # Face zones for canvas captions (same per-second data, canvas-relative)
  face_zones: list = []
  for sec in face_data:
    if not sec:
      face_zones.append(None)
    else:
      cys = sorted(f["cy"] for f in sec)
      med = cys[len(cys) // 2] / max(float(height), 1.0)
      face_zones.append("bottom" if med > 0.62 else ("top" if med < 0.35 else None))
  _verify_output(output_clip, cw, ch, duration, start)
  return {
    "file_path": output_clip,
    "start_time": start,
    "end_time": start + duration,
    "duration": duration,
    "title": suggestion.get("title", ""),
    "reason": suggestion.get("reason", ""),
    "viral_score": suggestion.get("viral_score", 0),
    "face_count": face_count,
    "layout_mode": layout,
    "aspect": comp["canvas"],
    "face_zone": None,
    "face_zones": face_zones,
    "cuts_applied": cuts_applied,
    "transcript": edited_transcript,
    "caption_window": caption_window,
    "needs_user_confirm": False,
    "reframed": True,
    "composition": comp,
  }


def _get_video_dimensions(video_path: str) -> tuple[int, int]:
  """Use ffprobe to get width and height."""
  ffprobe_path = resolve_ffprobe()
  cmd = [
      ffprobe_path,
      "-v", "error", "-select_streams", "v:0",
      "-show_entries", "stream=width,height", "-of", "json", video_path
  ]
  try:
    result = subprocess.run(cmd, capture_output=True, text=True)
  except FileNotFoundError as e:
    raise RuntimeError(
      f"ffprobe not found at '{ffprobe_path}'. Install FFmpeg "
      f"(winget install Gyan.FFmpeg) and restart the backend."
    ) from e
  if result.returncode != 0:
      raise RuntimeError(f"FFprobe failed: {result.stderr}")
  data = json.loads(result.stdout)
  stream = data["streams"][0]
  return stream["width"], stream["height"]

async def _cut_raw_clip(
  video_path: str,
  start: float,
  end: float,
  output_path: str
) -> None:
  """Frame-accurate cut (re-encoded) so clips never open frozen."""
  duration = end - start
  ffmpeg_path = resolve_ffmpeg()
  cmd = [
      ffmpeg_path, "-y", "-ss", str(start), "-i", video_path,
      "-t", str(duration),
      "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
      "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
      "-avoid_negative_ts", "make_zero", output_path
  ]
  try:
    result = subprocess.run(cmd, capture_output=True, text=True)
  except FileNotFoundError as e:
    raise RuntimeError(
      f"ffmpeg not found at '{ffmpeg_path}'. Install FFmpeg "
      f"(winget install Gyan.FFmpeg) and restart the backend."
    ) from e
  if result.returncode != 0:
      raise RuntimeError(f"FFmpeg failed cutting raw clip: {result.stderr}")
