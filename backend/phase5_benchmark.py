"""
phase5_benchmark.py — Phase 5: Real-video benchmarking and ablation studies.

Extends the benchmark dataset with authorized video import, dataset partitioning,
baseline benchmark runner, and ablation study support.

All data is local. No network. No paid APIs.
"""
from __future__ import annotations

import os
import json
import time
import hashlib
import logging
import subprocess
from typing import Any, Optional, Callable

import benchmark_dataset as bd

logger = logging.getLogger(__name__)


def import_authorized_video(
    base_dir: str,
    source_path: str,
    video_id: str,
    permission_status: str = "pending",
    permission_basis: str = "",
    media_type: str = "real",
    dataset_version: str = bd.DATASET_VERSION,
) -> bd.VideoMetadata:
    """Import an authorized video into the benchmark dataset."""
    if not os.path.exists(source_path):
        raise FileNotFoundError("Source file not found: %s" % source_path)
    if permission_status == "denied":
        raise ValueError("Cannot import video with denied permission status")

    source_hash = hashlib.sha256()
    with open(source_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            source_hash.update(chunk)
    file_hash = source_hash.hexdigest()

    meta = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height,r_frame_rate",
             "-show_entries", "format=duration", "-of", "json", source_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            import json as _json
            data = _json.loads(result.stdout)
            streams = data.get("streams", [])
            fmt = data.get("format", {})
            vstream = streams[0] if streams else {}
            fps_str = vstream.get("r_frame_rate", "0/1")
            if "/" in fps_str:
                num, den = fps_str.split("/")
                fps = float(num) / float(den) if float(den) > 0 else 0.0
            else:
                fps = float(fps_str) if fps_str else 0.0
            meta = {"duration": float(fmt.get("duration", 0)), "width": vstream.get("width", 0),
                    "height": vstream.get("height", 0), "fps": fps}
    except Exception as e:
        logger.warning("ffprobe failed: %s", e)

    has_audio = True
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", source_path],
            capture_output=True, text=True, timeout=15,
        )
        if "audio" not in (result.stdout or ""):
            has_audio = False
    except Exception:
        pass

    video_meta = bd.VideoMetadata(
        video_id=video_id, source_hash=file_hash, duration=meta["duration"],
        resolution="%dx%d" % (meta["width"], meta["height"]), fps=meta["fps"],
        has_audio=has_audio, has_transcript=False, permission_status=permission_status,
        permission_evidence=permission_basis, media_type=media_type,
        added_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        dataset_version=dataset_version,
    )
    bd.add_video(base_dir, video_meta, dataset_version)
    return video_meta


PARTITION_ALGORITHM_VERSION = "sha256-source-partition-v2"
PARTITION_SEED = "openclip-phase5.2-partition-seed-v1"

def assign_partition(source_hash, total_videos=0, dev_ratio=0.6, val_ratio=0.2, source_group_id=""):
    if not source_hash:
        raise ValueError("source_hash is required for partition assignment")
    identity = source_group_id if source_group_id else source_hash
    combined = (PARTITION_SEED + "|" + identity).encode("utf-8")
    h = int(hashlib.sha256(combined).hexdigest(), 16)
    bucket = h % 100
    if bucket < int(dev_ratio * 100):
        return "dev"
    elif bucket < int((dev_ratio + val_ratio) * 100):
        return "val"
    else:
        return "test"


def get_partition_videos(base_dir: str, partition: str, version: str = bd.DATASET_VERSION) -> list[dict]:
    """Get videos assigned to a partition."""
    videos = bd.get_videos(base_dir, version)
    return [v for v in videos if assign_partition(v["video_id"], len(videos)) == partition]


def create_partition_manifest(base_dir: str, version: str = bd.DATASET_VERSION) -> dict:
    """Create a manifest showing video-to-partition assignments."""
    videos = bd.get_videos(base_dir, version)
    manifest = {"dev": [], "val": [], "test": []}
    for v in videos:
        p = assign_partition(v["video_id"], len(videos))
        manifest[p].append(v["video_id"])
    return manifest


def run_ablation_study(video_path: str, output_dir: str, transcript: list[dict] | None = None) -> dict:
    """Run ablation study comparing different signal configurations."""
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")
    import evaluation_engine as ee
    import deterministic_candidate as dc

    meta = ee.get_video_metadata(video_path)
    duration = meta["duration"] or 30.0

    if transcript is None:
        transcript = []
        seg_dur = 5.0
        for i in range(int(duration / seg_dur)):
            transcript.append({"start": i * seg_dur, "end": (i + 1) * seg_dur, "text": "Segment %d content." % (i + 1)})

    audio_sigs = {"loudness": [], "burst": [], "silence": []}
    visual_sigs = {"motion": [], "scene_cut": []}
    try:
        import signals as sig_mod
        audio_sigs = sig_mod.extract_audio_signals(video_path, duration)
        visual_sigs = sig_mod.extract_visual_signals(video_path, duration)
    except Exception as e:
        logger.warning("Signal extraction failed: %s", e)

    configs = {
        "transcript_only": (None, None),
        "audio_only": (audio_sigs, None),
        "visual_only": (None, visual_sigs),
        "full_multimodal": (audio_sigs, visual_sigs),
    }

    results = {}
    for name, (asig, vsig) in configs.items():
        cands = dc.generate_candidates(transcript, duration, audio_sigs=asig, visual_sigs=vsig)
        results[name] = {"candidate_count": len(cands)}

    ablation_path = os.path.join(output_dir, "ablation_%s.json" % os.path.splitext(os.path.basename(video_path))[0])
    bd._atomic_write_json(ablation_path, results)
    return results
