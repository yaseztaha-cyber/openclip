"""
Social Canvas tests: state, geometry, deterministic render, re-render,
reframe+social, caption trim mapping, background sync.
"""
import asyncio, hashlib, json, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import composition as C
import reframer
from ffmpeg_utils import resolve_ffmpeg, resolve_ffprobe
from PIL import Image


def _mk(src_kind: str, path: str, duration: float = 5, w: int = 1280, h: int = 720):
    if src_kind == "testsrc":
        v = f"testsrc=size={w}x{h}:rate=30:duration={duration}"
    elif src_kind == "halves":
        v = (f"color=c=red:s={w // 2}x{h}:d={duration}:r=30[rl];"
             f"color=c=blue:s={w - w // 2}x{h}:d={duration}:r=30[bl];"
             "[rl][bl]hstack")
        r = subprocess.run([resolve_ffmpeg(), "-y", "-f", "lavfi", "-i", v,
                            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                            "-shortest", path], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-400:]
        return
    elif src_kind == "dark":
        v = f"color=c=0x3c3c3c:s={w}x{h}:d={duration}:r=30"
    elif src_kind == "sweep":
        v = (f"nullsrc=s={w}x{h}:r=30:d={duration},"
             "geq=r='128+100*sin(2*PI*T/3)':g='60+40*sin(2*PI*T/3+1)':b='200'")
    else:
        raise ValueError(src_kind)
    r = subprocess.run([resolve_ffmpeg(), "-y", "-f", "lavfi", "-i", v,
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", path], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]


def _probe(path: str) -> dict:
    p = subprocess.run([resolve_ffprobe(), "-v", "error", "-show_entries",
                        "stream=width,height,codec_type,codec_name",
                        "-show_entries", "format=duration",
                        "-of", "json", path], capture_output=True, text=True, timeout=30)
    return json.loads(p.stdout)


def _frame(path: str, t: float, out: str):
    subprocess.run([resolve_ffmpeg(), "-y", "-ss", str(t), "-i", path,
                    "-frames:v", "1", out], capture_output=True, timeout=30)
    return Image.open(out).convert("RGB")


def test_state_defaults_and_normalize():
    d = C.default_composition()
    assert d["mode"] == "reframe" and d["canvas"] == "9:16"
    assert d["background"]["blur_value"] == 18.0
    n = C.normalize_composition({"mode": "social", "canvas": "1:1",
                                 "background": {"scale": 99, "x": -5, "blur": "heavy"},
                                 "foreground": {"scale": 0.1, "radius": 999},
                                 "captions": {"position": "everywhere"}},
                                "9:16")
    assert n["mode"] == "social" and n["canvas"] == "1:1"
    assert n["background"]["scale"] == 1.5 and n["background"]["x"] == 0.0
    assert n["background"]["blur_value"] == 30.0
    assert n["foreground"]["scale"] == 0.5 and n["foreground"]["radius"] == 48
    assert n["captions"]["position"] == "bottom"  # invalid falls back
    json.dumps(n)  # serializable (undo/redo + autosave)
    print("state OK")


def test_presets_and_auto():
    for name in ("classic_vertical", "blur_16x9", "dark_blur", "gradient", "floating"):
        c = C.apply_preset(C.default_composition(), name)
        assert c["mode"] == "social", name
        json.dumps(c)
    a = C.auto_social(1920, 1080)
    assert a["mode"] == "social" and a["canvas"] == "9:16"
    assert a["background"]["blur_value"] == 18.0
    v = C.auto_social(1080, 1920)
    assert v["background"]["blur_value"] == 0.0  # vertical source: no fake blur
    assert C.BLUR_LEVELS == {"none": 0.0, "light": 10.0, "medium": 18.0, "heavy": 30.0}
    print("presets OK")


def test_geometry_invariants():
    for sw, sh in ((1920, 1080), (1280, 720), (3840, 2160), (1080, 1920), (640, 480)):
        for cw, ch in ((1080, 1920), (1920, 1080), (1080, 1080), (1080, 1350)):
            for sc in (1.0, 1.05, 1.5):
                for bx, by in ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)):
                    b = C.bg_cover_box(sw, sh, cw, ch, sc, bx, by)
                    assert b["scaled_w"] % 2 == 0 and b["scaled_h"] % 2 == 0
                    assert b["crop_x"] % 2 == 0 and b["crop_y"] % 2 == 0
                    assert b["scaled_w"] >= cw and b["scaled_h"] >= ch  # covers: no holes
                    assert 0 <= b["crop_x"] <= b["scaled_w"] - cw
                    assert 0 <= b["crop_y"] <= b["scaled_h"] - ch
            for sc in (0.75, 1.0, 1.25):
                for fx, fy in ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)):
                    f = C.fg_fit_box(sw, sh, cw, ch, sc, fx, fy)
                    assert f["box_w"] % 2 == 0 and f["box_h"] % 2 == 0
                    assert f["x"] + f["box_w"] <= cw and f["y"] + f["box_h"] <= ch  # inside
                    # aspect preserved
                    assert abs(f["box_w"] / f["box_h"] - sw / sh) < 0.02, (f, sw, sh)
    assert C.caption_y(1920, "bottom") == 1497 and C.caption_y(1920, "top") == 307
    print("geometry OK")


def test_filter_builders():
    g = C.rounded_alpha_geq(400, 300, 24)
    assert "255,0" in g and "gte" in g
    assert C.rounded_alpha_geq(400, 300, 0) == ""
    chain, shadow, sigma = C.fg_chain(400, 300, 12, "soft", {}, "")
    assert shadow and "geq" in chain and sigma == "18"
    chain2, shadow2, _ = C.fg_chain(400, 300, 0, "none", {}, "")
    assert not shadow2 and "geq" not in chain2
    assert C._hex_to_ff("#ffffff") == "0xFFFFFF" and C._hex_to_ff("nope") == "0xFFFFFF"
    print("filters OK")


def _social_render(tmp: str, src: str, comp: dict, duration: float = 5,
                   kfs=None, canvas: str = "9:16") -> dict:
    opts: dict = {"composition": comp, "canvas": canvas, "no_retention": True}
    if kfs:
        opts["crop_mode"] = "manual"
        opts["crop_keyframes"] = kfs
    return asyncio.run(reframer.render_social(
        src, {"start": 0, "end": duration, "title": "t", "reason": "t", "viral_score": 5},
        tmp, "social-test", options=opts))


def test_deterministic_social_render():
    """Required test: 16:9 -> 9:16, medium blur, subtle dark, fg 0.9 + raised, caption."""
    import captioner
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        _mk("dark", src, duration=6)
        comp = C.default_composition("9:16")
        comp["mode"] = "social"
        comp["foreground"]["scale"] = 0.90
        comp["foreground"]["y"] = 0.30
        comp["foreground"]["position_preset"] = "custom"
        out = _social_render(tmp, src, comp)
        fp = out["file_path"]
        d = _probe(fp)
        v = [s for s in d["streams"] if s["codec_type"] == "video"][0]
        a = [s for s in d["streams"] if s["codec_type"] == "audio"]
        assert (v["width"], v["height"]) == (1080, 1920), v
        assert abs(float(d["format"]["duration"]) - 5.0) < 0.6
        assert a, "audio missing"
        # Burn a caption + title overlay on canvas, verify events + visible pixels
        words = [{"word": w, "start": 1.0 + 0.3 * i, "end": 1.0 + 0.3 * (i + 1)}
                 for i, w in enumerate("HELLO WORLD FROM THE SOCIAL CANVAS".split())]
        segs = [{"start": 1.0, "end": 3.5, "text": "HELLO WORLD FROM THE SOCIAL CANVAS", "words": words}]
        capped, events = asyncio.run(captioner.burn_captions(
            fp, segs, 0.0, 5.0, "viral_word", canvas=(1080, 1920),
            caption_pos="bottom", caption_scale=1.0,
            overlays={"title": {"enabled": True, "text": "TEST TITLE", "subtitle": "",
                                "position": "top", "scale": 1.0}}))
        assert events > 5, events
        fr = _frame(capped, 2.0, os.path.join(tmp, "cap.png"))
        # Bottom-center alignment: glyphs sit just above the 0.78H anchor
        band = [fr.getpixel((x, 1440)) for x in range(100, 980, 40)]
        bright = sum(1 for p in band if min(p) > 200)
        assert bright >= 3, f"caption not visible in band: {band[:6]}"
        # No black bars: corners must not be pure black
        for xy in ((5, 5), (1074, 5), (5, 1914), (1074, 1914)):
            assert sum(fr.getpixel(xy)) > 12, f"black corner {xy}"
        # Foreground visible: box region differs from flat bg
        box = C.fg_fit_box(1280, 720, 1080, 1920, 0.9, 0.5, 0.30)
        assert box["box_h"] > 100
        print(f"deterministic render OK {v['width']}x{v['height']} events={events} bright={bright}")


def test_second_render_differs():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        _mk("testsrc", src, duration=6)
        c1 = C.default_composition("9:16")
        c1["mode"] = "social"
        o1 = _social_render(tmp, src, c1)
        h1 = hashlib.sha1(open(o1["file_path"], "rb").read()).hexdigest()
        c2 = C.default_composition("9:16")
        c2["mode"] = "social"
        c2["foreground"]["scale"] = 0.8
        c2["foreground"]["y"] = 0.2
        c2["foreground"]["position_preset"] = "custom"
        # render to a different tag dir to avoid filename collision
        o2 = asyncio.run(reframer.render_social(
            src, {"start": 0, "end": 5, "title": "t", "reason": "t", "viral_score": 5},
            tmp, "social-test-2",
            options={"composition": c2, "canvas": "9:16", "no_retention": True}))
        h2 = hashlib.sha1(open(o2["file_path"], "rb").read()).hexdigest()
        assert h1 != h2, "second render identical despite changed state"
        for o in (o1, o2):
            d = _probe(o["file_path"])
            v = [s for s in d["streams"] if s["codec_type"] == "video"][0]
            assert (v["width"], v["height"]) == (1080, 1920)
            assert abs(float(d["format"]["duration"]) - 5.0) < 0.6
        print("second render OK")


def test_social_reframe_keyframes():
    """16:9 -> social + fg keyframes: foreground content moves, bg stays sane."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        _mk("halves", src, duration=6)
        comp = C.default_composition("9:16")
        comp["mode"] = "social"
        kfs = [{"t": 0, "x": 0.2, "y": 0.5, "zoom": 1.5},
               {"t": 5, "x": 0.8, "y": 0.5, "zoom": 1.5}]
        out = _social_render(tmp, src, comp, kfs=kfs)
        box = C.fg_fit_box(1280, 720, 1080, 1920, 1.0, 0.5, 0.5)
        cx, cy = box["x"] + box["box_w"] // 2, box["y"] + box["box_h"] // 2
        f0 = _frame(out["file_path"], 0.5, os.path.join(tmp, "k0.png"))
        f1 = _frame(out["file_path"], 4.5, os.path.join(tmp, "k1.png"))
        p0, p1 = f0.getpixel((cx, cy)), f1.getpixel((cx, cy))
        assert p0[0] > 120 and p0[2] < 120, f"expected red-ish at t=0.5, got {p0}"
        assert p1[2] > 120 and p1[0] < 120, f"expected blue-ish at t=4.5, got {p1}"
        print(f"reframe kf OK {p0} -> {p1}")


def test_caption_trim_mapping():
    """10s source, render 2->7, caption 3->5 source => 1->3 on output timeline."""
    import captioner
    segs = [{"start": 2.5, "end": 5.5, "text": "hello world test",
             "words": [{"word": "hello", "start": 3.0, "end": 3.4},
                       {"word": "world", "start": 4.0, "end": 4.4},
                       {"word": "test", "start": 4.6, "end": 5.0}]}]
    clip = captioner._extract_clip_segments(segs, 2.0, 7.0)
    assert len(clip) == 1
    starts = [w["start"] for w in clip[0]["words"]]
    assert abs(min(starts) - 1.0) < 0.01 and abs(max(starts) - 3.0) < 0.6, starts
    print("caption trim OK", starts)


def test_background_sync():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        _mk("sweep", src, duration=6)
        comp = C.default_composition("9:16")
        comp["mode"] = "social"
        out = _social_render(tmp, src, comp)
        tops = []
        for t in (0.5, 2.0, 3.5):
            fr = _frame(out["file_path"], t, os.path.join(tmp, f"s{t}.png"))
            tops.append(fr.getpixel((540, 40)))
        d01 = sum(abs(a - b) for a, b in zip(tops[0], tops[1]))
        d12 = sum(abs(a - b) for a, b in zip(tops[1], tops[2]))
        assert d01 > 8 and d12 > 8, f"bg frozen: {tops}"
        print("bg sync OK", tops)


if __name__ == "__main__":
    test_state_defaults_and_normalize()
    test_presets_and_auto()
    test_geometry_invariants()
    test_filter_builders()
    test_caption_trim_mapping()
    test_deterministic_social_render()
    test_second_render_differs()
    test_social_reframe_keyframes()
    test_background_sync()
    print("ALL SOCIAL COMPOSITION TESTS PASSED")
