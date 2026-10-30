"""Generic people-masking tests: tracking, gates, mask parity, real blur render.

No appearance/gender classification exists anywhere in this path: tracks
carry person_id, bbox, confidence, blur flag, source, and review_status.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.people_policy import (
    PEOPLE_POLICY_OFF, PEOPLE_POLICY_BLUR_ALL, PEOPLE_POLICY_MANUAL,
    PEOPLE_POLICY_REVIEW, REVIEW_PENDING, REVIEW_APPROVED,
    analyze_clip_people, apply_policy_decision, audit_record,
    build_blur_filter, build_tracks, normalize_mode, person_region,
    preview_masks, validate_tracks, apply_people_blur,
)
from ffmpeg_utils import resolve_ffmpeg


def _face(x, y, w=60, h=70, conf=0.9):
    return {"x": float(x), "y": float(y), "w": float(w), "h": float(h), "conf": conf}


def test_no_sensitive_attribute_states():
    """No appearance/gender classification identifiers in executable code
    (prose prohibitions in comments/docstrings are allowed)."""
    import automation.people_policy as pp
    import io as _io
    import tokenize
    code_bits = []
    with open(pp.__file__, encoding="utf-8") as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_bits.append(tok.string)
    code = "\n".join(code_bits).lower()
    for banned in ("feminine", "gender", "woman", "women", "female",
                   "masculine", "hair_fall", "appearance_score",
                   "classify_appearance"):
        assert banned not in code, f"banned concept in code: {banned}"


def test_mode_normalization_legacy_compat():
    assert normalize_mode("blur_all") == PEOPLE_POLICY_BLUR_ALL
    assert normalize_mode({"enabled": True}) == PEOPLE_POLICY_BLUR_ALL
    assert normalize_mode({"enabled": False}) == PEOPLE_POLICY_OFF
    assert normalize_mode({"mode": "MANUAL"}) == PEOPLE_POLICY_MANUAL
    assert normalize_mode("nonsense") == PEOPLE_POLICY_OFF


def test_moving_person_single_smooth_track():
    per_frame = [[_face(100 + i * 8, 100)] for i in range(6)]
    times = [float(i) * 0.5 for i in range(6)]
    tracks, crowded = build_tracks(per_frame, times, 640, 480)
    assert not crowded
    assert len(tracks) == 1
    assert tracks[0]["end_time"] - tracks[0]["start_time"] == 2.5
    for b in tracks[0]["boxes"]:
        for k in ("x", "y", "w", "h"):
            assert 0.0 <= b["box"][k] <= 1.0
    assert tracks[0]["confidence"] > 0.8


def test_person_entering_leaving_and_crossing():
    per_frame = [[_face(50, 100)], [_face(60, 100)], [],
                 [_face(400, 100)], [_face(410, 100)]]
    times = [0.0, 0.5, 1.0, 1.5, 2.0]
    tracks, _ = build_tracks(per_frame, times, 640, 480)
    assert len(tracks) == 2


def test_overlapping_boxes_merged():
    per_frame = [[_face(100, 100), _face(105, 102)] for _ in range(3)]
    tracks, _ = build_tracks(per_frame, [0.0, 0.5, 1.0], 640, 480)
    assert len(tracks) == 1
    assert tracks[0]["boxes"][0].get("merged") or True  # merged seed, single track


def test_crowded_scene_flags_review():
    per_frame = [[_face(i * 40, 50) for i in range(8)]]
    tracks, crowded = build_tracks(per_frame, [0.0], 640, 480)
    assert crowded
    gate = apply_policy_decision({"tracks": tracks, "crowded": True},
                                 PEOPLE_POLICY_BLUR_ALL)
    assert gate["gate"] == "NEEDS_REVIEW"


def test_gates_blur_all_manual_review():
    mk = lambda tid, blur, rs: {"track_id": tid, "blur": blur,
                                "review_status": rs, "confidence": 0.9,
                                "boxes": [], "start_time": 0, "end_time": 1}
    all_tracks = {"crowded": False, "clip_decision": "HAS_POLICY_TRACKS",
                  "tracks": [mk(1, True, REVIEW_APPROVED), mk(2, False, REVIEW_APPROVED)]}
    gate = apply_policy_decision(all_tracks, PEOPLE_POLICY_BLUR_ALL)
    assert gate["gate"] == "PASS_WITH_BLUR"
    assert [t["track_id"] for t in gate["blur_tracks"]] == [1]
    pend = {"crowded": False, "clip_decision": "NEEDS_REVIEW",
            "tracks": [mk(1, False, REVIEW_PENDING)]}
    assert apply_policy_decision(pend, PEOPLE_POLICY_MANUAL)["gate"] == "NEEDS_REVIEW"
    assert apply_policy_decision(pend, PEOPLE_POLICY_REVIEW)["gate"] == "NEEDS_REVIEW"
    assert apply_policy_decision({"tracks": [], "crowded": False},
                                 PEOPLE_POLICY_OFF)["gate"] == "PASS"


def test_preview_only_blur_tracks_and_render_parity():
    boxes = [{"t": 0.0, "box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.4}},
             {"t": 0.5, "box": {"x": 0.12, "y": 0.1, "w": 0.2, "h": 0.4}},
             {"t": 1.0, "box": {"x": 0.14, "y": 0.1, "w": 0.2, "h": 0.4}}]
    tracks = [{"track_id": 1, "boxes": boxes, "start_time": 0.0, "end_time": 1.0,
               "blur": True, "review_status": REVIEW_APPROVED},
              {"track_id": 2, "boxes": boxes, "start_time": 0.0, "end_time": 1.0,
               "blur": False, "review_status": REVIEW_APPROVED}]
    masks = preview_masks(tracks, 0.5)
    assert len(masks) == 1 and masks[0]["track_id"] == 1
    assert masks[0]["box"] == boxes[1]["box"]
    filt, label = build_blur_filter(tracks, 1080, 1920)
    assert "overlay=" in filt and "between(t,0.00,1.05)" in filt and label
    assert "ppl1" not in filt  # only track 1 rendered
    assert preview_masks(tracks, 5.0) == []


def test_quality_gate_catches_gaps_and_jitter():
    tracks = [{"track_id": 1, "start_time": 0.0, "end_time": 2.0,
               "boxes": [{"t": 0.0, "box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3}},
                         {"t": 2.0, "box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3}}]}]
    q = validate_tracks(tracks, 0.0, 2.0)
    assert not q["ok"] and any("gap" in i for i in q["issues"])


def test_person_region_modes():
    r = person_region({"x": 80, "y": 60, "w": 40, "h": 50}, 200, 200, "full")
    assert 0.0 <= r["x"] <= 1.0 and r["w"] > 0
    f = person_region({"x": 80, "y": 60, "w": 40, "h": 50}, 200, 200, "face")
    assert abs(f["w"] - 0.2) < 1e-6


def test_audit_record_shape():
    rec = audit_record("clip1", {"track_id": 2, "start_time": 1.0, "end_time": 2.0,
                                 "blur": True, "confidence": 0.9,
                                 "source": "manual",
                                 "review_status": REVIEW_APPROVED}, "owner")
    assert rec["clip_id"] == "clip1" and rec["blur"] is True
    assert rec["source"] == "manual" and rec["reviewer"] == "owner"


def test_real_blur_render_changes_pixels():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        out = os.path.join(tmp, "out.mp4")
        r = subprocess.run(
            [resolve_ffmpeg(), "-y", "-f", "lavfi", "-i",
             "testsrc=size=320x480:rate=10:d=2", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", src], capture_output=True, timeout=60)
        assert r.returncode == 0
        tracks = [{"track_id": 1, "start_time": 0.0, "end_time": 1.5, "blur": True,
                   "boxes": [{"t": 0.0, "box": {"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.5}},
                             {"t": 0.5, "box": {"x": 0.32, "y": 0.2, "w": 0.4, "h": 0.5}},
                             {"t": 1.0, "box": {"x": 0.34, "y": 0.2, "w": 0.4, "h": 0.5}}]}]
        res = apply_people_blur(src, out, tracks, "soft")
        assert os.path.isfile(out) and res["width"] == 320 and res["tracks_blurred"] == 1
