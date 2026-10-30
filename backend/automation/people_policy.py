"""
people_policy.py — GENERIC people-masking policy (Phase 6+7+8).

Allowed modes: OFF | BLUR_ALL | MANUAL | REVIEW.

  OFF    — no detection, no masking.
  BLUR_ALL — every reliably tracked person region is blurred at render.
  MANUAL — owner picks per-person [Blur]/[Keep Visible]; unreviewed tracks
           block auto-publishing (NEEDS_REVIEW).
  REVIEW — detection only; any detected person forces human review.

The system detects GENERIC person regions (face-box-derived estimates) and
tracks them. It NEVER classifies gender, age, ethnicity, or identity, and
uses no face geometry / hair / skin / clothing / voice / name signals for
decisions. Each track carries person_id, bbox, confidence, interval,
blur flag, source (automatic/manual), and review_status.

Render-time detection on final output pixels is AUTHORITATIVE. Preview
overlays are advisory and use the same normalized track data.

Canonical render order (documented + enforced by call order):
  SOURCE → TRIM → REFRAME/CROP → SOCIAL COMPOSITION → PEOPLE MASK →
  CAPTIONS → GRAPHICS → AUDIO → FINAL ENCODE
Preview and render coordinates both refer to the transformed (output)
coordinate space because render-time tracks are measured on final pixels.
"""

from __future__ import annotations

import io
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

# --- Policy modes (no sensitive-attribute states exist in this module) ---
PEOPLE_POLICY_OFF = "OFF"
PEOPLE_POLICY_BLUR_ALL = "BLUR_ALL"
PEOPLE_POLICY_MANUAL = "MANUAL"
PEOPLE_POLICY_REVIEW = "REVIEW"
PEOPLE_MODES = frozenset({PEOPLE_POLICY_OFF, PEOPLE_POLICY_BLUR_ALL,
                          PEOPLE_POLICY_MANUAL, PEOPLE_POLICY_REVIEW})

REVIEW_PENDING = "PENDING"
REVIEW_APPROVED = "APPROVED"
REVIEW_REJECTED = "REJECTED"

CROWDED_FACE_LIMIT = 6
IOU_MATCH_THRESHOLD = 0.25
IOU_MERGE_THRESHOLD = 0.6
MAX_TRACK_GAP_S = 0.6
MIN_TRACK_DETECTIONS = 2
LOW_CONFIDENCE_THRESHOLD = 0.45

BLUR_TYPES = {"soft", "strong", "pixelate", "mask"}


def normalize_mode(value) -> str:
    """Accept new mode strings plus legacy {enabled: bool} configs."""
    if isinstance(value, dict):
        if value.get("mode") in PEOPLE_MODES:
            return value["mode"]
        return PEOPLE_POLICY_BLUR_ALL if value.get("enabled") else PEOPLE_POLICY_OFF
    if isinstance(value, str) and value.upper() in PEOPLE_MODES:
        return value.upper()
    return PEOPLE_POLICY_OFF


# ---------------------------------------------------------------------------
# Frame extraction (ffmpeg → PIL, no cv2 dependency)
# ---------------------------------------------------------------------------

def extract_frames(video_path: str, start: float, end: float,
                   fps: float = 2.0, width: int = 640) -> tuple[list, int, int]:
    """Returns (frames, frame_w, frame_h) as PIL images."""
    from PIL import Image  # type: ignore
    dur = max(0.5, end - start)
    cmd = [resolve_ffmpeg(), "-v", "error", "-ss", f"{start:.2f}", "-i", video_path,
           "-t", f"{dur:.2f}", "-vf", f"fps={fps},scale={width}:-2",
           "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    raw = proc.stdout or b""
    frames: list = []
    pos = 0
    while True:
        s = raw.find(b"\xff\xd8", pos)
        if s == -1:
            break
        e = raw.find(b"\xff\xd9", s)
        if e == -1:
            break
        try:
            frames.append(Image.open(io.BytesIO(bytes(raw[s:e + 2]))).convert("RGB"))
        except Exception:
            pass
        pos = e + 2
    if frames:
        return frames, frames[0].width, frames[0].height
    return [], width, 0


def _get_face_detector():
    """Reuse the project's MediaPipe BlazeFace detector (lazy import)."""
    import reframer  # type: ignore
    return reframer.face_detector


def detect_face_boxes(frames: list, frame_w: int, frame_h: int) -> list[list[dict]]:
    """Per-frame generic person-region seeds (face boxes) in frame pixels."""
    import numpy as np  # type: ignore
    import mediapipe as mp  # type: ignore
    detector = _get_face_detector()
    out: list[list[dict]] = []
    for frame in frames:
        rgb = np.array(frame)
        try:
            res = detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        except Exception:
            out.append([])
            continue
        boxes = []
        for det in (res.detections or []):
            bb = det.bounding_box
            if bb.height / max(frame_h, 1) < 0.05:
                continue
            try:
                conf = float(det.score[0]) if getattr(det, "score", None) else 0.8
            except Exception:
                conf = 0.8
            boxes.append({"x": float(bb.origin_x), "y": float(bb.origin_y),
                          "w": float(bb.width), "h": float(bb.height),
                          "conf": conf})
        out.append(_merge_overlapping(boxes))
    return out


def _merge_overlapping(boxes: list[dict]) -> list[dict]:
    """Union-merge same-frame boxes with high IoU (duplicate seeds)."""
    merged: list[dict] = []
    for b in sorted(boxes, key=lambda d: -d.get("conf", 0)):
        placed = False
        for m in merged:
            if _iou(b, m) >= IOU_MERGE_THRESHOLD:
                m["x"] = min(m["x"], b["x"])
                m["y"] = min(m["y"], b["y"])
                x2 = max(m["x"] + m["w"], b["x"] + b["w"])
                y2 = max(m["y"] + m["h"], b["y"] + b["h"])
                m["w"], m["h"] = x2 - m["x"], y2 - m["y"]
                m["conf"] = max(m["conf"], b["conf"])
                m["merged"] = True
                placed = True
                break
        if not placed:
            merged.append(dict(b))
    return merged


# ---------------------------------------------------------------------------
# Person regions (face box → estimated person region, normalized 0..1)
# ---------------------------------------------------------------------------

def person_region(face: dict, frame_w: int, frame_h: int,
                  mode: str = "full") -> dict:
    """Expand a detection box to a person region. Modes: face | upper | full."""
    fx, fy, fw, fh = face["x"], face["y"], face["w"], face["h"]
    cx = fx + fw / 2
    if mode == "face":
        x, y, w, h = fx, fy, fw, fh
    elif mode == "upper":
        w = fw * 2.0
        x = cx - w / 2
        y = fy - fh * 0.4
        h = fh * 2.6
    else:  # full person region (default)
        w = fw * 2.4
        x = cx - w / 2
        y = fy - fh * 0.5
        h = fh * 5.0
    x = max(0.0, x)
    y = max(0.0, y)
    w = min(w, frame_w - x)
    h = min(h, frame_h - y)
    return {"x": x / frame_w, "y": y / frame_h,
            "w": max(w, 1.0) / frame_w, "h": max(h, 1.0) / frame_h}


# ---------------------------------------------------------------------------
# Tracking (greedy IoU + gap-fill interpolation + EMA smoothing)
# ---------------------------------------------------------------------------

def _iou(a: dict, b: dict) -> float:
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0.0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0.0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _lerp_box(a: dict, b: dict, f: float) -> dict:
    return {k: round(a[k] + (b[k] - a[k]) * f, 4) for k in ("x", "y", "w", "h")}


def build_tracks(face_per_frame: list[list[dict]], frame_times: list[float],
                 frame_w: int, frame_h: int, region_mode: str = "full") -> tuple[list[dict], bool]:
    """Greedy IoU tracking → persistent tracks with normalized person boxes."""
    tracks: list[dict] = []
    next_id = 1
    crowded = False
    for fi, faces in enumerate(face_per_frame):
        t = frame_times[fi]
        # Merge duplicate seeds first so one person never spawns two tracks.
        faces = _merge_overlapping(list(faces))
        if len(faces) > CROWDED_FACE_LIMIT:
            crowded = True
        order = sorted(range(len(faces)), key=lambda i: -faces[i].get("conf", 0))
        used_tracks: set[int] = set()
        for i in order:
            f = faces[i]
            person = person_region(f, frame_w, frame_h, region_mode)
            best, best_iou = None, 0.0
            for tr in tracks:
                if id(tr) in used_tracks:
                    continue
                iou = _iou(person, tr["boxes"][-1]["box"])
                if iou > best_iou:
                    best, best_iou = tr, iou
            if best is not None and best_iou >= IOU_MATCH_THRESHOLD:
                best["boxes"].append({"t": t, "box": person, "iou": round(best_iou, 3),
                                      "conf": round(float(f.get("conf", 0)), 3)})
                best["end_time"] = t
                used_tracks.add(id(best))
            else:
                tracks.append({"track_id": next_id, "start_time": t, "end_time": t,
                               "boxes": [{"t": t, "box": person, "iou": 0.0,
                                          "conf": round(float(f.get("conf", 0)), 3)}]})
                next_id += 1
    # Gap-fill interpolation (short detection gaps don't break the mask),
    # then EMA smoothing so masks glide instead of jittering.
    for tr in tracks:
        filled: list[dict] = []
        boxes = tr["boxes"]
        for i, b in enumerate(boxes):
            filled.append(b)
            if i + 1 < len(boxes):
                nxt = boxes[i + 1]
                gap = nxt["t"] - b["t"]
                if 0 < gap <= MAX_TRACK_GAP_S * 2 and gap > 1e-6:
                    mid_t = (b["t"] + nxt["t"]) / 2
                    filled.append({"t": round(mid_t, 3),
                                   "box": _lerp_box(b["box"], nxt["box"], 0.5),
                                   "iou": 0.0, "conf": 0.0, "interpolated": True})
        filled.sort(key=lambda d: d["t"])
        sm = None
        for b in filled:
            box = b["box"]
            if sm is None:
                sm = dict(box)
            else:
                for k in ("x", "y", "w", "h"):
                    sm[k] = 0.65 * sm[k] + 0.35 * box[k]
            b["box"] = {k: round(v, 4) for k, v in sm.items()}
        tr["boxes"] = filled
        confs = [b.get("conf", 0) for b in filled if not b.get("interpolated")]
        tr["confidence"] = round(sum(confs) / len(confs), 3) if confs else 0.0
    # Drop single-detection flicker.
    tracks = [tr for tr in tracks if len([b for b in tr["boxes"]
                                          if not b.get("interpolated")]) >= MIN_TRACK_DETECTIONS]
    for idx, tr in enumerate(tracks, 1):
        tr["track_id"] = idx
    return tracks, crowded


def analyze_clip_people(video_path: str, start: float, end: float,
                        region_mode: str = "full",
                        mode: str = PEOPLE_POLICY_BLUR_ALL,
                        manual_blur_ids: list[int] | None = None,
                        fps: float = 2.0) -> dict:
    """
    Full clip-level analysis. mode ∈ OFF|BLUR_ALL|MANUAL|REVIEW.
    Returns {"tracks": [...], "crowded": bool, "clip_decision": ...}.
    clip_decision ∈ CLEAR | NEEDS_REVIEW | HAS_POLICY_TRACKS.
    """
    mode = normalize_mode(mode)
    if mode == PEOPLE_POLICY_OFF:
        return {"tracks": [], "crowded": False, "clip_decision": "CLEAR",
                "reason": "people_policy_off"}
    frames, fw, fh = extract_frames(video_path, start, end, fps=fps)
    if not frames:
        return {"tracks": [], "crowded": False, "clip_decision": "NEEDS_REVIEW",
                "reason": "no_frames_extracted"}
    faces = detect_face_boxes(frames, fw, fh)
    step = (end - start) / max(len(frames), 1)
    times = [start + i * step for i in range(len(frames))]
    tracks, crowded = build_tracks(faces, times, fw, fh, region_mode)
    manual = set(manual_blur_ids or [])

    for tr in tracks:
        low_conf = tr.get("confidence", 0) < LOW_CONFIDENCE_THRESHOLD
        if mode == PEOPLE_POLICY_BLUR_ALL:
            tr["blur"] = True
            tr["review_status"] = REVIEW_APPROVED if not low_conf else REVIEW_PENDING
        elif mode == PEOPLE_POLICY_MANUAL:
            tr["blur"] = tr["track_id"] in manual
            tr["review_status"] = (REVIEW_APPROVED if tr["track_id"] in manual
                                   else REVIEW_PENDING)
        else:  # REVIEW — detection only, human decides everything
            tr["blur"] = False
            tr["review_status"] = REVIEW_PENDING
        tr["source"] = "automatic"
        if low_conf:
            tr["low_confidence"] = True

    if crowded:
        decision, reason = "NEEDS_REVIEW", "crowded_scene_tracking_unreliable"
    elif any(t.get("low_confidence") for t in tracks) and mode == PEOPLE_POLICY_BLUR_ALL:
        decision, reason = "NEEDS_REVIEW", "low_confidence_tracks_require_review"
    elif mode == PEOPLE_POLICY_REVIEW and tracks:
        decision, reason = "NEEDS_REVIEW", "review_mode_requires_human_decision"
    elif mode == PEOPLE_POLICY_MANUAL and any(
            t["review_status"] == REVIEW_PENDING for t in tracks):
        decision, reason = "NEEDS_REVIEW", "unreviewed_tracks_require_decision"
    elif any(t.get("blur") for t in tracks):
        decision, reason = "HAS_POLICY_TRACKS", "policy_tracks_require_blur"
    else:
        decision, reason = "CLEAR", "no_policy_tracks"
    return {"tracks": tracks, "crowded": crowded,
            "clip_decision": decision, "reason": reason,
            "frame_w": fw, "frame_h": fh, "mode": mode}


# ---------------------------------------------------------------------------
# Policy gate + quality gate
# ---------------------------------------------------------------------------

def apply_policy_decision(analysis: dict, mode=PEOPLE_POLICY_BLUR_ALL) -> dict:
    """Map analysis → clip gate. Unresolved clips must not auto-publish."""
    mode = normalize_mode(mode)
    if mode == PEOPLE_POLICY_OFF:
        return {"gate": "PASS", "reason": "people_policy_off", "blur_tracks": []}
    if analysis.get("crowded"):
        return {"gate": "NEEDS_REVIEW", "reason": "crowded_scene", "blur_tracks": []}
    blur_tracks = [t for t in analysis.get("tracks", []) if t.get("blur")]
    pending = [t for t in analysis.get("tracks", [])
               if t.get("review_status") == REVIEW_PENDING]
    if analysis.get("clip_decision") == "NEEDS_REVIEW":
        return {"gate": "NEEDS_REVIEW",
                "reason": analysis.get("reason", "unresolved_tracks"),
                "blur_tracks": blur_tracks,
                "pending": [t.get("track_id") for t in pending]}
    if blur_tracks:
        return {"gate": "PASS_WITH_BLUR", "reason": "blur_policy_tracks",
                "blur_tracks": blur_tracks}
    return {"gate": "PASS", "reason": "no_policy_tracks", "blur_tracks": []}


def validate_tracks(tracks: list[dict], clip_start: float, clip_end: float) -> dict:
    """Quality gate: continuity, coverage, jitter. Unstable → NEEDS_REVIEW."""
    issues: list[str] = []
    dur = max(clip_end - clip_start, 0.01)
    covered = 0.0
    for tr in tracks:
        boxes = tr.get("boxes", [])
        real = [b for b in boxes if not b.get("interpolated")]
        if len(real) < MIN_TRACK_DETECTIONS:
            issues.append(f"track {tr.get('track_id')}: too_short")
            continue
        gaps = [boxes[i + 1]["t"] - boxes[i]["t"] for i in range(len(boxes) - 1)]
        if gaps and max(gaps) > MAX_TRACK_GAP_S * 2:
            issues.append(f"track {tr.get('track_id')}: gap {max(gaps):.2f}s breaks continuity")
        covered += max(0.0, tr.get("end_time", 0) - tr.get("start_time", 0))
        moves = []
        for i in range(len(boxes) - 1):
            a, b = boxes[i]["box"], boxes[i + 1]["box"]
            moves.append(math.hypot((a["x"] + a["w"] / 2) - (b["x"] + b["w"] / 2),
                                    (a["y"] + a["h"] / 2) - (b["y"] + b["h"] / 2)))
        if moves and sum(moves) / len(moves) > 0.25:
            issues.append(f"track {tr.get('track_id')}: unstable_jitter")
        # trim-boundary sanity: boxes must carry timestamps inside the clip
        if boxes and (boxes[0]["t"] < clip_start - 0.01 or boxes[-1]["t"] > clip_end + 0.51):
            issues.append(f"track {tr.get('track_id')}: outside_trim_bounds")
    coverage = min(1.0, covered / dur) if tracks else 1.0
    if tracks and coverage < 0.3:
        issues.append("low_coverage")
    return {"ok": not issues, "issues": issues, "coverage": round(coverage, 3)}


def audit_record(clip_id: str, track: dict, reviewer: str | None = None) -> dict:
    return {"clip_id": clip_id, "track_id": track.get("track_id"),
            "start_time": track.get("start_time"), "end_time": track.get("end_time"),
            "blur": bool(track.get("blur")), "confidence": track.get("confidence"),
            "source": track.get("source", "automatic"),
            "review_status": track.get("review_status", REVIEW_PENDING),
            "reviewer": reviewer}


# ---------------------------------------------------------------------------
# FFmpeg blur render (same normalized tracks as the browser preview)
# ---------------------------------------------------------------------------

def preview_masks(tracks: list[dict], at_time: float) -> list[dict]:
    """Normalized boxes active at t — drives the browser preview overlay."""
    out = []
    for tr in tracks:
        if not tr.get("blur"):
            continue
        boxes = tr.get("boxes", [])
        if not boxes:
            continue
        if at_time < boxes[0]["t"] or at_time > boxes[-1]["t"]:
            continue
        best = min(boxes, key=lambda b: abs(b["t"] - at_time))
        out.append({"track_id": tr["track_id"], "box": best["box"],
                    "review_status": tr.get("review_status")})
    return out


def _blur_chain(filter_in: str, box_expr: dict, W: int, H: int,
                blur_type: str, enable_expr: str, tag: str) -> tuple[str, str]:
    """One person-region blur stage. Returns (filter_snippet, new_label)."""
    x = f"({box_expr['x']}*{W})"
    y = f"({box_expr['y']}*{H})"
    w = f"({box_expr['w']}*{W})"
    h = f"({box_expr['h']}*{H})"
    en = f"enable='{enable_expr}'"
    if blur_type == "pixelate":
        inner = (f"crop={w}:{h}:{x}:{y},scale=iw/12:ih/12,scale=iw*12:ih*12:flags=neighbor")
    elif blur_type == "mask":
        return (f"{filter_in}drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill"
                f":{en}[{tag}o]", f"[{tag}o]")
    else:  # soft / strong
        radius = 40 if blur_type == "strong" else 20
        inner = f"crop={w}:{h}:{x}:{y},boxblur={radius}:2"
    return (f"split[base_{tag}][c_{tag}];[c_{tag}]{inner}[b_{tag}];"
            f"[base_{tag}][b_{tag}]overlay={x}:{y}:{en}[{tag}o]",
            f"[{tag}o]")


def build_blur_filter(tracks: list[dict], W: int, H: int,
                      blur_type: str = "soft") -> tuple[str, str]:
    """
    Chained filter_complex blurring every blur-flagged track over its own
    time ranges. Contiguous detections are grouped so the blur follows
    movement (no stationary rectangle left behind).
    Returns (filter_complex, out_label).
    """
    if blur_type not in BLUR_TYPES:
        blur_type = "soft"
    stages: list[str] = []
    label = "[0:v]"
    n = 0
    for tr in [t for t in tracks if t.get("blur")]:
        boxes = tr.get("boxes", [])
        if not boxes:
            continue
        groups: list[list[dict]] = [[boxes[0]]]
        for b in boxes[1:]:
            if b["t"] - groups[-1][-1]["t"] <= MAX_TRACK_GAP_S * 2 + 1e-6:
                groups[-1].append(b)
            else:
                groups.append([b])
        for g in groups:
            rep = sorted(g, key=lambda b: b["box"]["w"] * b["box"]["h"])[len(g) // 2]["box"]
            a, b = g[0]["t"], g[-1]["t"]
            enable = f"between(t,{a:.2f},{b + 0.05:.2f})"
            snippet, label = _blur_chain(label, rep, W, H, blur_type, enable, f"ppl{n}")
            stages.append(snippet)
            n += 1
    return (";".join(stages), label if stages else "")


def apply_people_blur(input_path: str, output_path: str, tracks: list[dict],
                      blur_type: str = "soft") -> dict:
    """Render people-blur masks into a real output file via FFmpeg."""
    import json as _json
    try:
        from ffmpeg_utils import resolve_ffprobe as _resolve_ffprobe
    except ImportError:
        _resolve_ffprobe = None  # type: ignore
    ffprobe = _resolve_ffprobe() if _resolve_ffprobe else "ffprobe"
    probe = subprocess.run(
        [ffprobe, "-v", "error",
         "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=30)
    try:
        streams = _json.loads(probe.stdout or "{}").get("streams", [])
        W, H = int(streams[0]["width"]), int(streams[0]["height"])
    except Exception as exc:
        raise RuntimeError(f"people-blur probe failed: {exc}") from exc
    filt, out_label = build_blur_filter(tracks, W, H, blur_type)
    if not filt:
        raise RuntimeError("no blur tracks to render")
    cmd = [resolve_ffmpeg(), "-v", "error", "-y", "-i", input_path,
           "-filter_complex", filt + f",{out_label}format=yuv420p[v]",
           "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "18",
           "-preset", "veryfast", "-c:a", "aac", output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not os.path.isfile(output_path):
        raise RuntimeError(f"people-blur render failed: {(proc.stderr or '')[-800:]}")
    return {"output_path": output_path, "width": W, "height": H,
            "tracks_blurred": len([t for t in tracks if t.get("blur")]),
            "blur_type": blur_type}
