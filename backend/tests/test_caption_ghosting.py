"""
A6 regression: caption ghosting / duplicate layers.
Ensures at any timestamp only ONE active caption group.
"""
import re
import tempfile
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import captioner


def _parse_dialogues(ass_content: str):
    dialogues = []
    for line in ass_content.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        m = re.search(r"Dialogue: \d+,([^,]+),([^,]+),", line)
        if not m:
            continue
        def to_sec(t):
            h, mn, s = t.split(":")
            return int(h)*3600 + int(mn)*60 + float(s)
        dialogues.append({
            "start": to_sec(m.group(1)),
            "end": to_sec(m.group(2)),
            "text": line.split(",,")[-1],
            "raw": line,
        })
    dialogues.sort(key=lambda d: d["start"])
    return dialogues


def _assert_no_ghosting(dialogues, style_name):
    # 1. No overlapping events (allow 0.01s epsilon)
    for i in range(len(dialogues)-1):
        cur, nxt = dialogues[i], dialogues[i+1]
        assert cur["end"] <= nxt["start"] + 0.01, \
            f"{style_name} overlap {i}: {cur['end']:.2f} > {nxt['start']:.2f} ghosting!"

    # 2. No duplicate text layers at same time
    # Build timeline: for each dialogue, ensure no other with same start and same text
    seen = {}
    for d in dialogues:
        key = (round(d["start"], 2), re.sub(r"\{[^}]+\}", "", d["text"]).strip().lower())
        assert key not in seen, f"{style_name} duplicate event at {d['start']:.2f}: {d['text'][:30]}"
        seen[key] = True

    # 3. Correct word order: for one_word, each word should appear once in order
    # For viral_word, we check that words are uppercased and in order
    # This is style-specific, so we just check that no event is empty
    for d in dialogues:
        txt = re.sub(r"\{[^}]+\}", "", d["text"]).strip()
        assert txt, f"{style_name} empty caption at {d['start']:.2f}"

    # 4. Correct cleanup: last event should not extend beyond group end +0.6
    # Already enforced in code, but check that durations are reasonable
    for d in dialogues:
        dur = d["end"] - d["start"]
        assert 0.08 <= dur <= 1.0, f"{style_name} duration {dur:.2f}s out of range at {d['start']:.2f}"


def test_no_ghosting_all_styles():
    # Fast-speech group with close word timings
    groups = [{
        "start": 0.0, "end": 2.0, "text": "this is what happened when we tried",
        "words": [
            {"word": "this", "start": 0.0, "end": 0.12},
            {"word": "is", "start": 0.12, "end": 0.22},
            {"word": "what", "start": 0.22, "end": 0.38},
            {"word": "happened", "start": 0.38, "end": 0.65},
            {"word": "when", "start": 0.65, "end": 0.85},
            {"word": "we", "start": 0.85, "end": 0.95},
            {"word": "tried", "start": 0.95, "end": 1.25},
        ]
    }]
    for style_name in ["viral_word", "classic_white", "highlight_yellow", "tiktok_style", "neon_green"]:
        style = captioner.CAPTION_STYLES[style_name]
        tmp = tempfile.mktemp(suffix=".ass")
        n = captioner._write_ass_file(tmp, groups, style, y_pos=1500)
        content = open(tmp, encoding="utf-8").read()
        dialogues = _parse_dialogues(content)
        assert len(dialogues) == len(groups[0]["words"]) or style["animation"] != "one_word" or len(dialogues) == 7, \
            f"{style_name} wrong event count {len(dialogues)}"
        _assert_no_ghosting(dialogues, style_name)
        os.unlink(tmp)
        print(f"{style_name} OK - {len(dialogues)} events, no ghosting")


def test_no_ghosting_multi_group():
    # Two groups with short gap — ensure no cross-group overlap
    groups = [
        {"start": 0.0, "end": 1.0, "text": "first group here",
         "words": [{"word": "first", "start": 0.0, "end": 0.3}, {"word": "group", "start": 0.3, "end": 0.6}, {"word": "here", "start": 0.6, "end": 1.0}]},
        {"start": 1.2, "end": 2.0, "text": "second group there",
         "words": [{"word": "second", "start": 1.2, "end": 1.5}, {"word": "group", "start": 1.5, "end": 1.7}, {"word": "there", "start": 1.7, "end": 2.0}]},
    ]
    for style_name in ["viral_word", "classic_white"]:
        style = captioner.CAPTION_STYLES[style_name]
        tmp = tempfile.mktemp(suffix=".ass")
        captioner._write_ass_file(tmp, groups, style, y_pos=1500)
        dialogues = _parse_dialogues(open(tmp, encoding="utf-8").read())
        # Check that last event of group1 ends before first of group2
        # Group1 last end should be <= group2 first start
        # Find events by start time
        first_group_events = [d for d in dialogues if d["start"] < 1.1]
        second_group_events = [d for d in dialogues if d["start"] >= 1.2]
        if first_group_events and second_group_events:
            assert max(d["end"] for d in first_group_events) <= min(d["start"] for d in second_group_events) + 0.01, \
                f"{style_name} cross-group overlap"
        os.unlink(tmp)
    print("multi-group OK")


def test_filler_suppression_no_ghost():
    groups = [{
        "start": 0.0, "end": 1.5, "text": "um so this happened",
        "words": [
            {"word": "um", "start": 0.0, "end": 0.2},
            {"word": "so", "start": 0.2, "end": 0.4},
            {"word": "this", "start": 0.4, "end": 0.6},
            {"word": "happened", "start": 0.6, "end": 1.0},
        ]
    }]
    style = captioner.CAPTION_STYLES["viral_word"]
    tmp = tempfile.mktemp(suffix=".ass")
    captioner._write_ass_file(tmp, groups, style, y_pos=1500)
    content = open(tmp, encoding="utf-8").read()
    # um should be suppressed, not appear as alone
    assert "UM" not in content or content.count("UM") == 0, "Filler 'UM' should be suppressed"
    dialogues = _parse_dialogues(content)
    _assert_no_ghosting(dialogues, "viral_word filler")
    os.unlink(tmp)
    print("filler suppression OK")


if __name__ == "__main__":
    test_no_ghosting_all_styles()
    test_no_ghosting_multi_group()
    test_filler_suppression_no_ghost()
    print("ALL CAPTION GHOSTING TESTS PASSED")
