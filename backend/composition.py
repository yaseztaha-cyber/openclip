"""
composition.py — Social Canvas composition model + FFmpeg filter builders.

Single source of truth for the 16:9 -> 9:16 social composition:
  blurred duplicate background (same source, same time) + aspect-preserving
  foreground + captions + overlays, all driven by one normalized state dict
  shared by browser preview and FFmpeg render.

State ranges (all normalized, resolution-independent):
  mode: "reframe" | "social"
  canvas: "9:16" | "16:9" | "1:1" | "4:5"
  background: {mode, blur, brightness, contrast, opacity, scale, x, y,
               tint, tint_opacity, gradient, gradient_top, gradient_bottom,
               gradient_opacity, follow_foreground, motion}
  foreground: {scale, x, y, radius, shadow, border{enabled,width,color,opacity},
               fit ("fit"|"reframe")}
  captions: {position (top|center|bottom|custom|off), y, scale, style}
  overlays: {progress{enabled,position,color}, watermark{...}, title{...}}
"""

import math
from typing import Any, Optional

CANVAS_PRESETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}
DEFAULT_CANVAS = "9:16"

# blur preset name -> gblur sigma (None preset = 0)
BLUR_LEVELS: dict[str, float] = {"none": 0.0, "light": 10.0, "medium": 18.0, "heavy": 30.0}
# darkness preset name -> eq brightness offset
DARKNESS_LEVELS: dict[str, float] = {"natural": 0.0, "subtle": -0.06, "dark": -0.14, "very_dark": -0.25}

BG_MODES = ("blurred_video", "darkened_video", "solid_color", "gradient")
FG_FITS = ("fit", "reframe")
SHADOWS = ("none", "soft", "strong")
CAPTION_POSITIONS = ("top", "center", "bottom", "custom", "off")
BG_MOTIONS = ("none", "slow_zoom_in", "slow_zoom_out", "pan_left", "pan_right")


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except Exception:
        f = lo
    return max(lo, min(f, hi))


def _even(n: float) -> int:
    return max(2, int(round(n))) & ~1


def default_composition(canvas: str = DEFAULT_CANVAS) -> dict:
    """Fresh Social Canvas state (reframe mode off by default mode field)."""
    return {
        "mode": "reframe",
        "canvas": canvas if canvas in CANVAS_PRESETS else DEFAULT_CANVAS,
        "background": {
            "mode": "blurred_video",
            "blur": "medium",
            "blur_value": BLUR_LEVELS["medium"],
            "darkness": "subtle",
            "brightness": 0.0,       # slider -0.3..0.3 added to darkness offset
            "contrast": 1.0,         # 0.8..1.2
            "opacity": 1.0,
            "scale": 1.05,           # 1.0..1.5
            "x": 0.5, "y": 0.5,
            "tint": "none",          # none | black | custom
            "tint_color": "#000000",
            "tint_opacity": 0.0,
            "gradient": False,
            "gradient_top": "#000000",
            "gradient_bottom": "#000000",
            "gradient_opacity": 0.35,
            "follow_foreground": False,
            "motion": "none",
            "visible": True,
            "solid_color": "#0a0a0f",
        },
        "foreground": {
            "fit": "fit",            # fit = whole frame; reframe = crop window
            "scale": 1.0,            # 0.75..1.25
            "x": 0.5, "y": 0.5,
            "position_preset": "center",  # top|center|bottom|custom
            "radius": 0,             # 0..48 px (at 1080 wide)
            "shadow": "soft",
            "border": {"enabled": False, "width": 4, "color": "#ffffff", "opacity": 1.0},
        },
        "captions": {
            "position": "bottom",    # top|center|bottom|custom|off
            "y": 0.78,               # custom 0..1
            "scale": 1.0,            # 0.7..1.5 fontsize multiplier
            "safe_area": True,       # show guide in preview (never rendered)
        },
        "overlays": {
            "progress": {"enabled": False, "position": "bottom", "color": "#ffffff", "height": 8},
            "watermark": {"enabled": False, "kind": "text", "text": "",
                          "image": None, "position": "top-right",
                          "scale": 1.0, "opacity": 0.6},
            "title": {"enabled": False, "text": "", "subtitle": "",
                      "position": "top", "scale": 1.0},
        },
    }


def normalize_composition(comp: Optional[dict], canvas: str = DEFAULT_CANVAS) -> dict:
    """Clamp/validate a composition dict. Unknown fields pass through."""
    base = default_composition(canvas)
    if not isinstance(comp, dict):
        return base
    out = dict(base)
    if comp.get("mode") in ("reframe", "social"):
        out["mode"] = comp["mode"]
    if comp.get("canvas") in CANVAS_PRESETS:
        out["canvas"] = comp["canvas"]

    bg = dict(base["background"])
    if isinstance(comp.get("background"), dict):
        src = comp["background"]
        if src.get("mode") in BG_MODES:
            bg["mode"] = src["mode"]
        if src.get("blur") in BLUR_LEVELS:
            bg["blur"] = src["blur"]
            bg["blur_value"] = BLUR_LEVELS[src["blur"]]
        bg["blur_value"] = _clamp(src.get("blur_value", bg["blur_value"]), 0.0, 40.0)
        if src.get("darkness") in DARKNESS_LEVELS:
            bg["darkness"] = src["darkness"]
        bg["brightness"] = _clamp(src.get("brightness", 0.0), -0.3, 0.3)
        bg["contrast"] = _clamp(src.get("contrast", 1.0), 0.8, 1.2)
        bg["opacity"] = _clamp(src.get("opacity", 1.0), 0.0, 1.0)
        bg["scale"] = _clamp(src.get("scale", 1.05), 1.0, 1.5)
        bg["x"] = _clamp(src.get("x", 0.5), 0.0, 1.0)
        bg["y"] = _clamp(src.get("y", 0.5), 0.0, 1.0)
        if src.get("tint") in ("none", "black", "custom"):
            bg["tint"] = src["tint"]
        bg["tint_color"] = str(src.get("tint_color", bg["tint_color"]))[:16]
        bg["tint_opacity"] = _clamp(src.get("tint_opacity", 0.0), 0.0, 0.8)
        bg["gradient"] = bool(src.get("gradient", False))
        bg["gradient_top"] = str(src.get("gradient_top", bg["gradient_top"]))[:16]
        bg["gradient_bottom"] = str(src.get("gradient_bottom", bg["gradient_bottom"]))[:16]
        bg["gradient_opacity"] = _clamp(src.get("gradient_opacity", 0.35), 0.0, 0.8)
        bg["follow_foreground"] = bool(src.get("follow_foreground", False))
        if src.get("motion") in BG_MOTIONS:
            bg["motion"] = src["motion"]
        bg["visible"] = bool(src.get("visible", True))
        bg["solid_color"] = str(src.get("solid_color", bg["solid_color"]))[:16]
    out["background"] = bg

    fg = dict(base["foreground"])
    if isinstance(comp.get("foreground"), dict):
        src = comp["foreground"]
        if src.get("fit") in FG_FITS:
            fg["fit"] = src["fit"]
        fg["scale"] = _clamp(src.get("scale", 1.0), 0.5, 1.5)
        fg["x"] = _clamp(src.get("x", 0.5), 0.0, 1.0)
        fg["y"] = _clamp(src.get("y", 0.5), 0.0, 1.0)
        if src.get("position_preset") in ("top", "center", "bottom", "custom"):
            fg["position_preset"] = src["position_preset"]
            if fg["position_preset"] == "top":
                fg["y"] = 0.0
            elif fg["position_preset"] == "bottom":
                fg["y"] = 1.0
            elif fg["position_preset"] == "center":
                fg["y"] = 0.5
        fg["radius"] = _clamp(src.get("radius", 0), 0, 48)
        if src.get("shadow") in SHADOWS:
            fg["shadow"] = src["shadow"]
        bd = dict(fg["border"])
        if isinstance(src.get("border"), dict):
            b = src["border"]
            bd["enabled"] = bool(b.get("enabled", False))
            bd["width"] = _clamp(b.get("width", 4), 1, 24)
            bd["color"] = str(b.get("color", "#ffffff"))[:16]
            bd["opacity"] = _clamp(b.get("opacity", 1.0), 0.0, 1.0)
        fg["border"] = bd
    out["foreground"] = fg

    cap = dict(base["captions"])
    if isinstance(comp.get("captions"), dict):
        src = comp["captions"]
        if src.get("position") in CAPTION_POSITIONS:
            cap["position"] = src["position"]
        cap["y"] = _clamp(src.get("y", 0.78), 0.0, 1.0)
        cap["scale"] = _clamp(src.get("scale", 1.0), 0.7, 1.5)
        cap["safe_area"] = bool(src.get("safe_area", True))
    out["captions"] = cap

    ov = {"progress": dict(base["overlays"]["progress"]),
          "watermark": dict(base["overlays"]["watermark"]),
          "title": dict(base["overlays"]["title"])}
    if isinstance(comp.get("overlays"), dict):
        src = comp["overlays"]
        if isinstance(src.get("progress"), dict):
            p = src["progress"]
            ov["progress"]["enabled"] = bool(p.get("enabled", False))
            if p.get("position") in ("top", "bottom"):
                ov["progress"]["position"] = p["position"]
            ov["progress"]["color"] = str(p.get("color", "#ffffff"))[:16]
            ov["progress"]["height"] = int(_clamp(p.get("height", 8), 2, 24))
        if isinstance(src.get("watermark"), dict):
            w = src["watermark"]
            ov["watermark"]["enabled"] = bool(w.get("enabled", False))
            if w.get("kind") in ("text", "image"):
                ov["watermark"]["kind"] = w["kind"]
            ov["watermark"]["text"] = str(w.get("text", ""))[:120]
            ov["watermark"]["image"] = w.get("image")
            if w.get("position") in ("top-left", "top-right", "bottom-left", "bottom-right", "center"):
                ov["watermark"]["position"] = w["position"]
            ov["watermark"]["scale"] = _clamp(w.get("scale", 1.0), 0.25, 3.0)
            ov["watermark"]["opacity"] = _clamp(w.get("opacity", 0.6), 0.0, 1.0)
        if isinstance(src.get("title"), dict):
            t = src["title"]
            ov["title"]["enabled"] = bool(t.get("enabled", False))
            ov["title"]["text"] = str(t.get("text", ""))[:140]
            ov["title"]["subtitle"] = str(t.get("subtitle", ""))[:200]
            if t.get("position") in ("top", "center", "bottom"):
                ov["title"]["position"] = t["position"]
            ov["title"]["scale"] = _clamp(t.get("scale", 1.0), 0.5, 2.0)
    out["overlays"] = ov
    return out


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

SOCIAL_PRESETS: dict[str, dict] = {
    "classic_vertical": {"background": {"mode": "blurred_video", "blur": "medium",
                                        "darkness": "subtle", "scale": 1.05},
                         "foreground": {"scale": 1.0, "y": 0.5, "position_preset": "center",
                                        "radius": 0, "shadow": "soft"}},
    "blur_16x9": {"background": {"mode": "blurred_video", "blur": "medium",
                                 "darkness": "subtle", "scale": 1.08},
                  "foreground": {"scale": 1.0, "y": 0.5, "position_preset": "center",
                                 "radius": 0, "shadow": "soft"}},
    "dark_blur": {"background": {"mode": "blurred_video", "blur": "heavy",
                                 "darkness": "dark", "scale": 1.08},
                  "foreground": {"scale": 0.95, "y": 0.45, "position_preset": "custom",
                                 "radius": 16, "shadow": "strong"}},
    "gradient": {"background": {"mode": "blurred_video", "blur": "light",
                                "darkness": "subtle", "scale": 1.05,
                                "gradient": True, "gradient_opacity": 0.45},
                 "foreground": {"scale": 0.92, "y": 0.42, "position_preset": "custom",
                                "radius": 20, "shadow": "soft"}},
    "floating": {"background": {"mode": "darkened_video", "blur": "none",
                                "darkness": "dark", "scale": 1.0},
                 "foreground": {"scale": 0.88, "y": 0.5, "position_preset": "center",
                                "radius": 28, "shadow": "strong",
                                "border": {"enabled": True, "width": 3,
                                           "color": "#ffffff", "opacity": 0.85}}},
}


def apply_preset(comp: dict, name: str) -> dict:
    """Return a new normalized composition with preset values merged in."""
    out = normalize_composition(comp, (comp or {}).get("canvas", DEFAULT_CANVAS))
    preset = SOCIAL_PRESETS.get(name)
    if not preset:
        return out
    bg = dict(out["background"])
    for k, v in preset.get("background", {}).items():
        bg[k] = v
    if isinstance(preset["background"].get("blur"), str):
        bg["blur_value"] = BLUR_LEVELS[preset["background"]["blur"]]
    fg = dict(out["foreground"])
    for k, v in preset.get("foreground", {}).items():
        if k == "border" and isinstance(v, dict):
            bd = dict(fg["border"])
            bd.update(v)
            fg["border"] = bd
        else:
            fg[k] = v
    out["background"] = bg
    out["foreground"] = fg
    out["mode"] = "social"
    return normalize_composition(out, out["canvas"])


def auto_social(source_w: int, source_h: int) -> dict:
    """One-click AUTO SOCIAL: canvas + bg/fg chosen from source aspect."""
    comp = default_composition("9:16")
    comp["mode"] = "social"
    try:
        same = abs((source_w / max(source_h, 1)) - (1080 / 1920)) < 0.01
    except Exception:
        same = False
    if same:
        # Vertical source: plain full-frame, no fake blur needed
        comp["background"] = {**comp["background"], "mode": "blurred_video",
                              "blur": "none", "blur_value": 0.0, "scale": 1.0}
        comp["foreground"] = {**comp["foreground"], "scale": 1.0, "y": 0.5,
                              "position_preset": "center", "shadow": "none"}
    else:
        comp = apply_preset(comp, "blur_16x9")
    return comp


# ---------------------------------------------------------------------------
# Geometry (mirrored in frontend preview; even-pixel safe)
# ---------------------------------------------------------------------------

def canvas_size(canvas: str) -> tuple[int, int]:
    return CANVAS_PRESETS.get(canvas, CANVAS_PRESETS[DEFAULT_CANVAS])


def bg_cover_box(source_w: int, source_h: int, canvas_w: int, canvas_h: int,
                 scale: float, bx: float, by: float) -> dict:
    """Background: scale source to COVER canvas, crop overflow at (bx,by)."""
    s0 = max(canvas_w / max(source_w, 1), canvas_h / max(source_h, 1))
    s = s0 * _clamp(scale, 1.0, 1.5)
    scw = max(_even(source_w * s), canvas_w if canvas_w % 2 == 0 else canvas_w + 1)
    sch = max(_even(source_h * s), canvas_h if canvas_h % 2 == 0 else canvas_h + 1)
    ox = int(round(_clamp(bx, 0.0, 1.0) * max(scw - canvas_w, 0))) & ~1
    oy = int(round(_clamp(by, 0.0, 1.0) * max(sch - canvas_h, 0))) & ~1
    ox = max(0, min(ox, max(scw - canvas_w, 0)))
    oy = max(0, min(oy, max(sch - canvas_h, 0)))
    return {"scaled_w": scw, "scaled_h": sch, "crop_w": canvas_w,
            "crop_h": canvas_h, "crop_x": ox, "crop_y": oy}


def fg_fit_box(source_w: int, source_h: int, canvas_w: int, canvas_h: int,
               scale: float, fx: float, fy: float) -> dict:
    """Foreground: aspect-preserving fit box inside canvas at (fx,fy)."""
    base = min(canvas_w / max(source_w, 1), canvas_h / max(source_h, 1))
    bw = _even(source_w * base * _clamp(scale, 0.5, 1.5))
    bh = _even(bw * source_h / max(source_w, 1))
    if bw > canvas_w or bh > canvas_h:
        bh = min(bh, canvas_h if canvas_h % 2 == 0 else canvas_h - 1)
        bw = min(_even(bh * source_w / max(source_h, 1)), canvas_w if canvas_w % 2 == 0 else canvas_w - 1)
        bh = _even(bw * source_h / max(source_w, 1))
        if bh > canvas_h:
            bh = canvas_h & ~1
            bw = _even(bh * source_w / max(source_h, 1))
    ox = int(round(_clamp(fx, 0.0, 1.0) * max(canvas_w - bw, 0)))
    oy = int(round(_clamp(fy, 0.0, 1.0) * max(canvas_h - bh, 0)))
    return {"box_w": max(2, bw), "box_h": max(2, bh), "x": ox, "y": oy}


def caption_y(canvas_h: int, position: str, custom_y: float = 0.78) -> int:
    if position == "top":
        return int(canvas_h * 0.16)
    if position == "center":
        return int(canvas_h * 0.50)
    if position == "custom":
        return int(_clamp(custom_y, 0.0, 1.0) * canvas_h)
    return int(canvas_h * 0.78)  # bottom default


def effective_brightness(bg: dict) -> float:
    """Slider brightness + darkness preset offset, clamped to safe range."""
    return _clamp(DARKNESS_LEVELS.get(bg.get("darkness", "subtle"), -0.06)
                  + float(bg.get("brightness", 0.0)), -0.45, 0.25)


def bg_motion_keyframes(motion: str, duration: float) -> list[dict]:
    """Background motion presets as synthetic {t,x,y,zoom} keyframes reusing
    the standard interpolation (zoom = cover-scale multiplier, x/y = crop pos)."""
    if motion not in BG_MOTIONS or motion == "none" or duration <= 0:
        return []
    d = max(duration, 0.1)
    if motion == "slow_zoom_in":
        return [{"t": 0.0, "x": 0.5, "y": 0.5, "zoom": 1.0},
                {"t": d, "x": 0.5, "y": 0.5, "zoom": 1.12}]
    if motion == "slow_zoom_out":
        return [{"t": 0.0, "x": 0.5, "y": 0.5, "zoom": 1.12},
                {"t": d, "x": 0.5, "y": 0.5, "zoom": 1.0}]
    if motion == "pan_left":
        return [{"t": 0.0, "x": 0.75, "y": 0.5, "zoom": 1.0},
                {"t": d, "x": 0.25, "y": 0.5, "zoom": 1.0}]
    # pan_right
    return [{"t": 0.0, "x": 0.25, "y": 0.5, "zoom": 1.0},
            {"t": d, "x": 0.75, "y": 0.5, "zoom": 1.0}]


# ---------------------------------------------------------------------------
# FFmpeg filter builders (pure strings; tested in test_social_composition.py)
# ---------------------------------------------------------------------------

def _hex_to_ff(color: str) -> str:
    c = str(color or "#ffffff").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in c):
        c = "ffffff"
    return "0x" + c.upper()


def rounded_alpha_geq(box_w: int, box_h: int, radius: int) -> str:
    """geq alpha expression for a rounded-rect mask (same math as preview)."""
    r = max(0, min(int(radius), min(box_w, box_h) // 2))
    if r <= 0:
        return ""
    W, H, R = box_w, box_h, r
    tl = f"if(lte((R-X)*(R-X)+(R-Y)*(R-Y),R*R),255,0)"
    tr = f"if(lte((X-W+R)*(X-W+R)+(R-Y)*(R-Y),R*R),255,0)"
    bl = f"if(lte((R-X)*(R-X)+(Y-H+R)*(Y-H+R),R*R),255,0)"
    br = f"if(lte((X-W+R)*(X-W+R)+(Y-H+R)*(Y-H+R),R*R),255,0)"
    expr = (f"if(lt(X,{R})*lt(Y,{R}),{tl},"
            f"if(gte(X,{W-R})*lt(Y,{R}),{tr},"
            f"if(lt(X,{R})*gte(Y,{H-R}),{bl},"
            f"if(gte(X,{W-R})*gte(Y,{H-R}),{br},255))))")
    return expr.replace("W", str(W)).replace("H", str(H)).replace("R", str(R))


def fg_chain(box_w: int, box_h: int, radius: int, shadow: str,
             border: dict, grade_suffix: str = "") -> tuple[str, bool, str]:
    """Foreground video chain AFTER scale=box (input [x] -> output [fg]).
    Returns (filter_string, needs_shadow_branch, shadow_sigma)."""
    parts = [f"format=rgba"]
    if radius > 0:
        geq = rounded_alpha_geq(box_w, box_h, radius)
        parts.append(f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{geq}'")
    if border and border.get("enabled"):
        w = int(_clamp(border.get("width", 4), 1, 24))
        op = _clamp(border.get("opacity", 1.0), 0.0, 1.0)
        parts.append(f"drawbox=x=0:y=0:w=iw:h=ih:c={_hex_to_ff(border.get('color', '#ffffff'))}@{op}:t={w}")
    if grade_suffix:
        parts.append(grade_suffix.lstrip(","))
    sigma = "0"
    if shadow == "soft":
        sigma = "18"
    elif shadow == "strong":
        sigma = "32"
    return (",".join(parts), shadow in ("soft", "strong"), sigma)


def shadow_chain(sigma: str, dx: int = 0, dy: int = 14) -> tuple[str, int, int]:
    """Black-silhouette blur for the shadow branch (keeps fg alpha shape)."""
    return (f"format=rgba,colorchannelmixer=rr=0:gg=0:bb=0:aa=0.45,"
            f"gblur=sigma={sigma}", dx, dy)


def drawtext_filter(text: str, canvas_w: int, canvas_h: int, y_frac: float,
                    fontsize: int, fontcolor: str = "white",
                    box: bool = True) -> str:
    """Single drawtext filter with escaped text."""
    safe = str(text or "").replace("\\", "\\\\").replace(":", "\\:").replace("'", "")
    safe = safe.replace("\n", " ")
    box_s = ":box=1:boxcolor=black@0.55:boxborderw=24" if box else ""
    return (f"drawtext=font='Arial':text='{safe}':fontsize={fontsize}:"
            f"fontcolor={fontcolor}:x=(w-text_w)/2:y={y_frac:.3f}*h{box_s}")
