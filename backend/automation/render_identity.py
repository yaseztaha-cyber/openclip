"""
render_identity.py — Deterministic render identity (Phase 9).

render_hash covers every render-affecting input and nothing else:
  source identity, trim, keyframes, composition, captions, people policy +
  people masks, title/subtitle, output settings.

No timestamps, no random salts. Identical inputs → identical hash, so a
valid existing artifact can be reused; any render-affecting change produces
a new identity (which invalidates prior approvals bound to the old hash).
"""

from __future__ import annotations

import hashlib
import json


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def compute_render_hash(*, source_id: str, source_start: float, source_end: float,
                        crop_keyframes=None, composition=None, caption_style=None,
                        caption_enabled: bool = True, caption_scale: float = 1.0,
                        people_policy=None, people_masks=None,
                        title: str = "", subtitle: str = "",
                        output_aspect: str = "9:16", export_preset=None) -> str:
    doc = {
        "source_id": str(source_id),
        "trim": [round(float(source_start), 3), round(float(source_end), 3)],
        "crop_keyframes": crop_keyframes or [],
        "composition": composition or {},
        "captions": {"style": caption_style, "enabled": bool(caption_enabled),
                     "scale": float(caption_scale)},
        "people_policy": people_policy or {},
        "people_masks": people_masks or [],
        "title": title or "",
        "subtitle": subtitle or "",
        "output_aspect": output_aspect,
        "export_preset": export_preset,
    }
    return "rh_" + hashlib.sha256(_canon(doc).encode("utf-8")).hexdigest()[:24]


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:24]


def verify_render_artifact(path: str, *, min_duration: float = 0.5,
                           expect_aspect: str | None = None) -> dict:
    """
    ffprobe-based artifact verification: container readable, video+audio
    streams, sane dims/duration/size. Returns {ok, reasons[], meta}.
    """
    import os
    import subprocess
    reasons: list[str] = []
    meta: dict = {}
    try:
        from ffmpeg_utils import resolve_ffprobe
        ffprobe = resolve_ffprobe()
    except Exception:
        ffprobe = "ffprobe"
    if not path or not os.path.isfile(path):
        return {"ok": False, "reasons": ["file_missing"], "meta": meta}
    if os.path.getsize(path) < 10 * 1024:
        reasons.append("file_too_small")
    try:
        import json as _json
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=width,height,codec_type,codec_name,avg_frame_rate",
             "-show_entries", "format=duration,size",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        data = _json.loads(proc.stdout or "{}")
        vids = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        auds = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if not vids:
            reasons.append("no_video_stream")
        else:
            w, h = int(vids[0].get("width", 0)), int(vids[0].get("height", 0))
            meta.update(width=w, height=h, vcodec=vids[0].get("codec_name"))
            if w <= 0 or h <= 0:
                reasons.append("bad_dimensions")
            if expect_aspect:
                try:
                    aw, ah = expect_aspect.split(":")
                    if abs((w / max(h, 1)) - (float(aw) / float(ah))) > 0.02:
                        reasons.append(f"aspect_mismatch_{w}x{h}_want_{expect_aspect}")
                except Exception:
                    pass
        if not auds:
            reasons.append("no_audio_stream")
        dur = float((data.get("format") or {}).get("duration", 0) or 0)
        meta["duration"] = dur
        if dur < min_duration:
            reasons.append(f"duration_too_short_{dur:.2f}s")
    except Exception as exc:
        reasons.append(f"probe_failed_{exc}")
    return {"ok": not reasons, "reasons": reasons, "meta": meta}
