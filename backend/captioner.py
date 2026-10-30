import subprocess
import os
import sys
import json
import re
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List

try:
    from ffmpeg_utils import resolve_ffmpeg
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ffmpeg_utils import resolve_ffmpeg

import logging
logger = logging.getLogger(__name__)

CAPTION_STYLES: Dict[str, Dict[str, Any]] = {
  "classic_white": {
    "name": "Classic White",
    "font": "Arial-Bold",
    "fontsize": 95,
    "primary_color": "&H00FFFFFF",   # white
    "outline_color": "&H00000000",   # black outline
    "outline_width": 5,
    "shadow": 2,
    "background": False,
    "animation": "word_by_word",
    "max_chars_per_line": 15,
    "highlight_color": None
  },
  "yellow_bold": {
    "name": "Yellow Bold",
    "font": "Arial-Bold",
    "fontsize": 105,
    "primary_color": "&H0000FFFF",   # yellow
    "outline_color": "&H00000000",   # black
    "outline_width": 6,
    "shadow": 0,
    "background": False,
    "animation": "word_by_word",
    "max_chars_per_line": 14,
    "highlight_color": None
  },
  "neon_green": {
    "name": "Neon Green",
    "font": "Arial-Bold",
    "fontsize": 95,
    "primary_color": "&H0000FF66",   # neon green
    "outline_color": "&H00003300",   # dark green outline
    "outline_width": 5,
    "shadow": 2,
    "background": False,
    "animation": "highlight",
    "max_chars_per_line": 15,
    "highlight_color": "&H0000FF66"  # same green for highlight
  },
  "highlight_yellow": {
    "name": "Highlight Yellow",
    "font": "Arial-Bold",
    "fontsize": 95,
    "primary_color": "&H00FFFFFF",   # white default
    "outline_color": "&H00000000",
    "outline_width": 4,
    "shadow": 0,
    "background": True,
    "bg_color": "&H99000000",        # semi-transparent black bg
    "animation": "highlight",
    "max_chars_per_line": 15,
    "highlight_color": "&H0000FFFF"  # yellow highlight
  },
  "white_box": {
    "name": "White Box",
    "font": "Arial-Bold",
    "fontsize": 90,
    "primary_color": "&H00000000",   # black text
    "outline_color": "&H00FFFFFF",
    "outline_width": 0,
    "shadow": 0,
    "background": True,
    "bg_color": "&HFFFFFFFF",        # solid white background
    "animation": "word_by_word",
    "max_chars_per_line": 14,
    "highlight_color": None
  },
  "purple_glow": {
    "name": "Purple Glow",
    "font": "Arial-Bold",
    "fontsize": 100,
    "primary_color": "&H00FF66FF",   # purple/pink
    "outline_color": "&H00330033",
    "outline_width": 5,
    "shadow": 3,
    "background": False,
    "animation": "word_by_word",
    "max_chars_per_line": 14,
    "highlight_color": None
  },
  "tiktok_style": {
    "name": "TikTok Style",
    "font": "Arial-Bold",
    "fontsize": 110,
    "primary_color": "&H00FFFFFF",   # white
    "outline_color": "&H00000000",   # black
    "outline_width": 7,
    "shadow": 0,
    "background": False,
    "animation": "highlight",
    "max_chars_per_line": 12,
    "highlight_color": "&H0000FFFF"  # yellow highlight
  },
  "minimal_clean": {
    "name": "Minimal Clean",
    "font": "Arial",
    "fontsize": 46,
    "primary_color": "&H00FFFFFF",
    "outline_color": "&H00000000",
    "outline_width": 2,
    "shadow": 0,
    "background": False,
    "animation": "word_by_word",
    "max_chars_per_line": 28,
    "highlight_color": None
  },
  "viral_word": {
    "name": "Viral Word",
    "font": "Arial-Black",
    "fontsize": 95,
    "primary_color": "&H00FFFFFF",
    "outline_color": "&H00000000",
    "outline_width": 8,
    "shadow": 3,
    "shadow_color": "&H55000000",
    "background": False,
    "animation": "one_word",
    "max_chars_per_line": 8,
    "highlight_color": None,
    "uppercase": True,
    "letter_spacing": 2
  }
}

def _extract_clip_segments(transcript: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    result = []
    for seg in transcript:
        # Check overlap with clip window
        if seg['end'] < clip_start or seg['start'] > clip_end:
            continue
        adjusted = {
            "start": max(0.0, seg['start'] - clip_start),
            "end": min(clip_end - clip_start, seg['end'] - clip_start),
            "text": seg['text'].strip()
        }
        if adjusted['text'] and adjusted['end'] > adjusted['start']:
            if 'words' in seg:
                # filter and adjust word-level timestamps relative to clip_start
                adj_words = []
                for w in seg['words']: # type: ignore
                    w_start = w['start'] - clip_start
                    w_end = w['end'] - clip_start
                    if w_end > 0 and w_start < (clip_end - clip_start):
                        adj_words.append({
                            "word": w['word'],
                            "start": max(0.0, w_start),
                            "end": min(clip_end - clip_start, w_end)
                        })
                adjusted['words'] = adj_words
            
            result.append(adjusted)
    return result

def _build_caption_groups(segments: list[dict], max_chars: int) -> list[dict]:
    groups: List[Dict[str, Any]] = []
    current_text: str = ""
    current_start: float = 0.0
    current_end: float = 0.0
    current_words: list = []

    for seg in segments:
        words_data = seg.get('words')
        
        if words_data:
            # We have exact word-level timings from transcriber
            for w_data in words_data:
                word_str = str(w_data['word'])
                word_start = float(w_data['start'])
                word_end = float(w_data['end'])
                
                test_text = f"{current_text} {word_str}".strip()
                if len(test_text) > max_chars and current_text:
                    groups.append({
                        "start": current_start,
                        "end": current_end,
                        "text": current_text,
                        "words": current_words
                    })
                    current_text = word_str
                    current_start = word_start
                    current_end = word_end
                    current_words = [{
                        "word": word_str,
                        "start": word_start,
                        "end": word_end
                    }]
                else:
                    if not current_text:
                        current_start = word_start
                    current_text = test_text
                    current_end = max(current_end, word_end)
                    current_words.append({ # type: ignore
                        "word": word_str,
                        "start": word_start,
                        "end": word_end
                    })
            continue

        words: list[str] = seg['text'].split()
        seg_duration = float(seg['end']) - float(seg['start'])
        word_duration = seg_duration / max(len(words), 1)

        for i, word in enumerate(words):
            word_start = seg['start'] + i * word_duration
            word_end = word_start + word_duration

            word_str = str(word)
            test_text = f"{current_text} {word_str}".strip()

            if len(test_text) > max_chars and current_text:
                # Flush current group
                groups.append({
                    "start": current_start,
                    "end": current_end,
                    "text": current_text,
                    "words": current_words
                })
                current_text = word_str
                current_start = word_start
                current_end = word_end
                current_words = [{
                    "word": word_str,
                    "start": word_start,
                    "end": word_end
                }]
            else:
                if not current_text:
                    current_start = word_start
                current_text = test_text
                current_end = word_end
                current_words.append({ # type: ignore
                    "word": word_str,
                    "start": word_start,
                    "end": word_end
                })

    if current_text:
        groups.append({
            "start": current_start,
            "end": current_end,
            "text": current_text,
            "words": current_words
        })

    return groups

def _add_word_timing(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for group in groups:
        duration = group['end'] - group['start']
        words = group['words']
        if not words:
            continue

        # Weight by word length
        weights = []
        for w in words:
            l = len(w['word'])
            if l <= 2:
                weights.append(0.6)
            elif l >= 8:
                weights.append(1.3)
            else:
                weights.append(1.0)

        total_weight = sum(weights)
        t = float(group['start'])
        for i, (w, weight) in enumerate(zip(words, weights)):
            word_dur = float(duration) * (float(weight) / float(total_weight))
            # Only assign generated timing if actual word timings missing
            if w['start'] == w['end']: # meaning we built naive fallback
                w['start'] = float(t)
                w['end'] = float(t) + float(word_dur)
            t += word_dur

    return groups

def _write_ass_file(ass_path: str, groups: List[Dict[str, Any]], style: Dict[str, Any],
                    y_pos: int = 1500, y_positions: Optional[list[int]] = None,
                    play_res: tuple[int, int] = (1080, 1920), pos_x: Optional[int] = None,
                    extra_events: Optional[list[dict]] = None) -> int:
    """
    Write ASS events. Returns the number of Dialogue events written.
    Guards: min 0.3s duration, no overlap into the next group's start.
    If y_positions is provided, it is used per-group (P4 time-aware captions).
    play_res/pos_x adapt the header + \\pos anchor to any canvas (social mode);
    defaults preserve the legacy 1080x1920 output byte-for-byte.
    extra_events: [{start, end, text}] preformatted overlay lines (title,
    watermark) merged into the same deterministic timeline.
    """
    def seconds_to_ass(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    primary = style['primary_color']
    outline = style['outline_color']
    outline_w = style['outline_width']
    shadow = style['shadow']
    fontsize = style['fontsize']
    font = style['font']
    bold = -1  # bold on
    highlight = style.get('highlight_color', '&H0000FFFF')

    bg_color = style.get('bg_color', '&H00000000')
    border_style = 4 if style.get('background') else 1
    # Use shadow_color as BackColour when in outline mode (no background)
    back_colour = bg_color if style.get('background') else style.get('shadow_color', bg_color)
    spacing = style.get('letter_spacing', 0)

    prx, pry = int(play_res[0]), int(play_res[1])
    px = pos_x if pos_x is not None else prx // 2
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {prx}
PlayResY: {pry}
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{fontsize},{primary},{highlight},{outline},{back_colour},{bold},0,0,0,100,100,{spacing},0,{border_style},{outline_w},{shadow},2,80,80,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    events = 0

    # Collect events first for overlap diagnostics and deterministic timeline
    pending_events: list[dict] = []

    def _emit(start_time: float, end_time: float, text: str) -> None:
        """Collect event with no overlap guarantee — caller must ensure start<end."""
        nonlocal events
        if end_time <= start_time + 0.01:  # degenerate, drop
            return
        pending_events.append({"start": start_time, "end": end_time, "text": text})

    for g_idx, group in enumerate(groups):
        # P4: per-group y (time-aware) with fallback to global y_pos
        cur_y = y_positions[g_idx] if y_positions is not None and g_idx < len(y_positions) else y_pos
        animation = style['animation']

        if animation == 'word_by_word':
            shown_words = []
            for i, word_data in enumerate(group['words']):
                shown_words.append(word_data['word'])
                display_text = " ".join(shown_words)
                start_time = float(word_data['start'])
                if i + 1 < len(group['words']):
                    end_time = float(group['words'][i+1]['start'])
                else:
                    end_time = float(group['end'])
                    if g_idx + 1 < len(groups):
                        nxt = groups[g_idx+1].get('words', [])
                        if nxt:
                            end_time = min(end_time, float(nxt[0]['start']))
                    end_time = min(end_time, start_time + 0.6)
                # No artificial 0.30 extension for intra-group — prevents ghosting
                text = f"{{\\pos({px},{cur_y})}}{display_text}"
                _emit(start_time, end_time, text)

        elif animation == 'highlight':
            words = [w['word'] for w in group['words']]
            for i, word_data in enumerate(group['words']):
                start_time = float(word_data['start'])
                if i + 1 < len(group['words']):
                    end_time = float(group['words'][i+1]['start'])
                else:
                    end_time = float(group['end'])
                    if g_idx + 1 < len(groups):
                        nxt = groups[g_idx+1].get('words', [])
                        if nxt:
                            end_time = min(end_time, float(nxt[0]['start']))
                    end_time = min(end_time, start_time + 0.6)
                colored_parts: list[str] = []
                for j, w in enumerate(words):
                    if j == i:
                        colored_parts.append(f"{{\\1c{highlight}&\\bord{outline_w+1}}}{w}{{\\1c{primary}&\\bord{outline_w}}}")
                    else:
                        colored_parts.append(w)
                display_text = " ".join(colored_parts)
                text = f"{{\\pos({px},{cur_y})}}{display_text}"
                _emit(start_time, end_time, text)

        elif animation == 'one_word':
            _FILLER_ONLY = {"um", "uh", "erm", "ah", "hmm", "er", "mmm", "umm", "uhh", "ahh", "hmmm", "er-m"}
            kept: list = []
            pending_start: float | None = None
            for wd in group['words']:
                tok = str(wd.get('word', '')).strip().lower().strip(".,!?;:'\"")
                if tok in _FILLER_ONLY:
                    if pending_start is None:
                        pending_start = float(wd.get('start', 0.0))
                    continue
                if pending_start is not None:
                    wd = dict(wd)
                    wd['start'] = min(pending_start, float(wd.get('start', 0.0)))
                    pending_start = None
                kept.append(wd)
            for i, word_data in enumerate(kept):
                word_text = word_data['word'].upper()
                start_time = float(word_data['start'])
                if i + 1 < len(kept):
                    end_time = float(kept[i + 1]['start'])
                else:
                    end_time = float(group['end'])
                    if g_idx + 1 < len(groups):
                        nxt = groups[g_idx+1].get('words', [])
                        if nxt:
                            end_time = min(end_time, float(nxt[0]['start']))
                    end_time = min(end_time, start_time + 0.6)
                text = f"{{\\pos({px},{cur_y})\\fscx105\\fscy105}}{word_text}"
                _emit(start_time, end_time, text)

    # --- Overlay lines (title / text watermark): separate lane, never clipped
    # against captions (they intentionally span captions). Emitted with
    # Effect=overlay so validation treats them as a parallel lane.
    overlay_events: list[dict] = []
    if extra_events:
        for ev in extra_events:
            try:
                s, e = float(ev.get("start", 0.0)), float(ev.get("end", 0.0))
            except Exception:
                continue
            txt = str(ev.get("text", ""))
            if e > s + 0.01 and txt:
                overlay_events.append({"start": s, "end": e, "text": txt})

    # --- Deterministic timeline: sort, clip overlaps, enforce min 0.15 readable ---
    pending_events.sort(key=lambda e: e["start"])
    # Fix overlaps: each event ends exactly when next starts (no ghost, no gap)
    for i in range(len(pending_events) - 1):
        cur = pending_events[i]
        nxt = pending_events[i+1]
        if cur["end"] > nxt["start"]:
            cur["end"] = nxt["start"]
        # Enforce readable minimum 0.15s by extending only if gap allows
        min_dur = 0.15
        if cur["end"] - cur["start"] < min_dur:
            # If next is far, extend to min; otherwise keep as is (avoid overlap)
            if nxt["start"] - cur["start"] >= min_dur:
                cur["end"] = cur["start"] + min_dur
            # else keep short (fast speech) — no ghost is priority
    # Last event: enforce 0.30 min if gap to next group allows
    if pending_events:
        last = pending_events[-1]
        if last["end"] - last["start"] < 0.30:
            # Check gap to next group (if any) — we already limited to next group start
            last["end"] = last["start"] + 0.30

    # Emit to ASS (captions lane, then overlay lane)
    for ev in pending_events:
        if ev["end"] <= ev["start"]:
            continue
        lines.append(f"Dialogue: 0,{seconds_to_ass(ev['start'])},{seconds_to_ass(ev['end'])},Default,,0,0,0,,{ev['text']}\n")
        events += 1
    overlay_events.sort(key=lambda e: e["start"])
    for ev in overlay_events:
        lines.append(f"Dialogue: 0,{seconds_to_ass(ev['start'])},{seconds_to_ass(ev['end'])},Default,,0,0,0,overlay,{ev['text']}\n")
        events += 1

    # Diagnostics (A1)
    for i in range(len(pending_events)-1):
        if pending_events[i]["end"] > pending_events[i+1]["start"] + 0.01:
            logger.warning(f"Caption overlap {i}: {pending_events[i]['end']:.2f} > {pending_events[i+1]['start']:.2f}")
    # Duplicate text check
    seen = set()
    for ev in pending_events:
        txt = re.sub(r"\{[^}]+\}", "", ev["text"]).strip().lower()
        if txt in seen and len(txt) > 3:
            logger.debug(f"Duplicate caption text: {txt[:30]}")
        seen.add(txt)

    with open(ass_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    # A7: Production validation — abort if ghosting would be published
    # Parse back and verify no unintended overlap, duplicate, negative duration, chronology
    try:
        _validate_ass_file(ass_path)
    except Exception as e:
        # Remove invalid ASS to prevent publishing
        try:
            os.remove(ass_path)
        except: pass
        raise RuntimeError(f"Caption validation failed (ghosting guard): {e}") from e

    return events


def _validate_ass_file(ass_path: str) -> None:
    """A7: Verify no overlap, duplicate, negative duration, chronological order.

    Effect=overlay lines (title/watermark lane) are exempt from overlap,
    chronology and duplicate checks — they intentionally span captions —
    but must still have valid durations.
    """
    content = open(ass_path, encoding="utf-8").read()
    dialogues = []
    for line in content.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) < 10:
            continue
        def to_sec(t: str) -> str:
            h, mn, s = t.split(":")
            return int(h)*3600 + int(mn)*60 + float(s)
        try:
            start, end = to_sec(fields[1]), to_sec(fields[2])
        except Exception:
            continue
        effect = fields[8].strip()
        text = fields[9]
        if start < 0 or end < 0:
            raise ValueError(f"Negative timestamp: {start:.2f}->{end:.2f}")
        if end <= start:
            raise ValueError(f"Invalid duration: {start:.2f}->{end:.2f}")
        dialogues.append((start, end, text, effect))

    lanes = [d for d in dialogues if d[3] != "overlay"]
    lanes.sort(key=lambda x: x[0])
    for i in range(len(lanes)-1):
        cur_s, cur_e, cur_t, _ = lanes[i]
        nxt_s, nxt_e, nxt_t, _ = lanes[i+1]
        if cur_e > nxt_s + 0.01:
            raise ValueError(f"Overlap {i}: {cur_e:.2f} > {nxt_s:.2f} ghosting!")
        if cur_s > nxt_s + 0.01:
            raise ValueError(f"Chronology broken at {i}")
    # Duplicate text at same time
    seen = {}
    for s, e, t, _ in lanes:
        txt = re.sub(r"\{[^}]+\}", "", t).strip().lower()
        key = (round(s, 2), txt[:20])
        if txt and len(txt) > 3 and key in seen:
            raise ValueError(f"Duplicate event at {s:.2f}: {txt[:20]}")
        seen[key] = True

async def _burn_ass_to_video(input_path: str, ass_path: str, output_path: str) -> None:
    # Use backward slash since windows? But ffmpeg filters often require forward slashes for paths on Windows
    # to avoid escaping issues. Or we can use relative paths if we set cwd.
    # Let's fix the path for ffmpeg filter: replace \ with / and escape : as \:
    # Handle Windows drive letter for ffmpeg ass filter: C:\path -> C\:/path
    # Use forward slashes and escape only the drive colon, not UNC colons
    safe_ass_path = ass_path.replace("\\", "/")
    if len(safe_ass_path) >= 2 and safe_ass_path[1] == ":":
        safe_ass_path = safe_ass_path[0] + "\\:" + safe_ass_path[2:]
    ffmpeg_path = resolve_ffmpeg()
    cmd = [
        ffmpeg_path, "-i", input_path,
        "-vf", f"ass='{safe_ass_path}'",
        "-c:a", "copy",
        output_path, "-y"
    ]
    
    def _run_ffmpeg() -> tuple[int, str]:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as e:
            return 127, f"ffmpeg not found at '{ffmpeg_path}': {e}"
        return res.returncode, str(res.stderr)

    returncode, stderr_output = await asyncio.to_thread(_run_ffmpeg) # type: ignore

    if returncode != 0:
        if returncode == 127:
            raise RuntimeError(
                f"ffmpeg not found at '{ffmpeg_path}'. Install FFmpeg "
                f"(winget install Gyan.FFmpeg) and restart the backend."
            )
        raise RuntimeError(
            f"Caption burn failed: {stderr_output}"
        )

def _ass_escape(text: str) -> str:
    return str(text or "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def _overlay_events(overlays: Optional[dict], cw: int, ch: int, duration: float) -> list[dict]:
    """Title + text-watermark overlay-lane events (canvas-relative)."""
    if not isinstance(overlays, dict):
        return []
    out: list[dict] = []
    title = overlays.get("title") or {}
    if title.get("enabled") and str(title.get("text", "")).strip():
        try:
            tscale = max(0.5, min(float(title.get("scale", 1.0)), 2.0))
        except Exception:
            tscale = 1.0
        fs = int(round(ch * 0.042 * tscale))
        yfrac = {"top": 0.10, "center": 0.5, "bottom": 0.9}.get(str(title.get("position", "top")), 0.10)
        txt = _ass_escape(title["text"])
        sub = _ass_escape(title.get("subtitle", ""))
        line = f"{{\\pos({cw // 2},{int(ch * yfrac)})\\fs{fs}\\b1\\c&HFFFFFF&}}{txt}"
        if sub:
            line += f"\\N{{\\fs{max(20, int(fs * 0.55))}\\c&HDDDDDD&}}{sub}"
        out.append({"start": 0.0, "end": max(0.5, min(5.0, duration)), "text": line})
    wm = overlays.get("watermark") or {}
    if wm.get("enabled") and str(wm.get("kind", "text")) == "text" and str(wm.get("text", "")).strip():
        try:
            op = max(0.0, min(float(wm.get("opacity", 0.6)), 1.0))
        except Exception:
            op = 0.6
        alpha = f"&H{int(round((1.0 - op) * 255)):02X}&"
        pos = str(wm.get("position", "top-right"))
        wx = 60 if "left" in pos else (cw - 60 if "right" in pos else cw // 2)
        wy = 60 if "top" in pos else (ch - 60 if "bottom" in pos else ch // 2)
        out.append({"start": 0.0, "end": max(0.5, duration),
                    "text": f"{{\\pos({wx},{wy})\\fs{max(20, int(ch * 0.022))}\\alpha{alpha}}}"
                            f"{_ass_escape(wm['text'])}"})
    return out


async def burn_captions(video_path: str, full_transcript: List[Dict[str, Any]], clip_start: float, clip_end: float, style_key: str,
                      face_zone: str | None = None, face_zones: Optional[list] = None,
                      canvas: Optional[tuple[int, int]] = None,
                      caption_pos: Optional[str] = None,
                      caption_scale: float = 1.0,
                      overlays: Optional[dict] = None) -> tuple[str, int]:
    """
    Burn captions. Returns (output_path, dialogue_event_count).

    face_zone="bottom" (speaker low in frame) moves captions to the top
    third so they never cover the face; otherwise lower third.
    If face_zones (per-second list) is provided, y is chosen per caption
    interval with temporal smoothing to avoid jumping.

    Social Canvas: canvas=(w,h) sets PlayRes + center anchor + fontsize;
    caption_pos top|center|bottom|custom pins y to the canvas (face zones
    only apply to the legacy bottom default); caption_scale multiplies
    fontsize; overlays {title, watermark} add overlay-lane ASS events.
    """
    style = dict(CAPTION_STYLES.get(style_key, CAPTION_STYLES['classic_white']))
    cw, chh = (int(canvas[0]), int(canvas[1])) if canvas else (1080, 1920)
    try:
        cscale = float(caption_scale or 1.0)
    except Exception:
        cscale = 1.0
    cscale = max(0.7, min(cscale, 1.5)) * (chh / 1920.0)
    style["fontsize"] = max(20, int(round(int(style.get("fontsize", 95)) * cscale)))
    pos_x = cw // 2
    y_top, y_bottom = (500, 1500) if (canvas is None and chh == 1920) else (int(0.26 * chh), int(0.78 * chh))

    # Step 1 — Extract relevant transcript for this clip
    clip_segments = _extract_clip_segments(full_transcript, clip_start, clip_end)

    if not clip_segments:
        return video_path, 0  # no captions to add

    # Step 2 — Build caption groups
    max_chars = int(style['max_chars_per_line'])
    caption_groups = _build_caption_groups(clip_segments, max_chars)

    # Step 3 — Generate word timing if animation needs it
    if style['animation'] in ['word_by_word', 'highlight', 'one_word']:
        caption_groups = _add_word_timing(caption_groups)

    if not caption_groups:
        return video_path, 0

    # Step 4 — Build ASS file (face-aware placement, P4: per-second zones)
    ass_path = video_path + ".ass"
    pinned_y: Optional[int] = None
    if caption_pos == "top":
        pinned_y = y_top
    elif caption_pos == "center":
        pinned_y = int(chh * 0.50)
    elif caption_pos == "custom":
        pinned_y = int(chh * 0.78)
    if pinned_y is not None:
        y_positions = [pinned_y] * len(caption_groups)
        events = _write_ass_file(ass_path, caption_groups, style, y_positions=y_positions,
                                 play_res=(cw, chh), pos_x=pos_x,
                                 extra_events=_overlay_events(overlays, cw, chh, clip_end - clip_start))
    elif face_zones is not None:
        # Per-group y with temporal smoothing (avoid jumping every caption)
        y_positions = []
        last_y = y_top if face_zone == "bottom" else y_bottom
        last_zone: Optional[str] = face_zone
        pending_zone: Optional[str] = None
        for group in caption_groups:
            mid = (float(group.get("start", clip_start)) + float(group.get("end", clip_start))) / 2.0
            idx = int(mid - clip_start)
            zone = None
            if 0 <= idx < len(face_zones):
                zone = face_zones[idx]
            if zone is None:
                zone = face_zone
            # Smoothing: only switch if new zone persists, else keep last
            if zone != last_zone:
                if pending_zone == zone:
                    # Confirmed switch
                    last_zone = zone
                    last_y = y_top if zone == "bottom" else y_bottom
                    pending_zone = None
                else:
                    pending_zone = zone
                    # keep last_y
            else:
                pending_zone = None
            y_positions.append(last_y)
        events = _write_ass_file(ass_path, caption_groups, style, y_positions=y_positions,
                                 play_res=(cw, chh), pos_x=pos_x,
                                 extra_events=_overlay_events(overlays, cw, chh, clip_end - clip_start))
    else:
        y_pos = y_top if face_zone == "bottom" else y_bottom
        events = _write_ass_file(ass_path, caption_groups, style, y_pos=y_pos,
                                 play_res=(cw, chh), pos_x=pos_x,
                                 extra_events=_overlay_events(overlays, cw, chh, clip_end - clip_start))

    # Step 5 — Burn with FFmpeg
    root, ext = os.path.splitext(video_path)
    output_path = root + "_captioned" + ext
    try:
        await _burn_ass_to_video(video_path, ass_path, output_path)
    finally:
        try:
            if os.path.exists(ass_path):
                os.remove(ass_path)
        except OSError:
            pass
    return output_path, events
