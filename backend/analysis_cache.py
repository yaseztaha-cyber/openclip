"""
analysis_cache.py — Versioned analysis caching for OpenClip benchmark.

Caches transcription, audio signals, visual signals, candidates, and evaluation
results based on content hashes and configuration versions.

All cache operations are local. No network. No paid APIs.

Cache structure:
  data/evaluation/cache/
    <cache_type>/
      <cache_key>.json
"""
from __future__ import annotations

import os
import json
import hashlib
import time
import logging
from typing import Any, Optional, Callable
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

CACHE_VERSION = "phase5.3-v1"
CACHE_BASE = "data/evaluation/cache"


def _cache_dir(cache_type: str) -> str:
    return os.path.join(CACHE_BASE, cache_type)


def _cache_key(*components: str) -> str:
    """Compute a cache key from components."""
    combined = "|".join(str(c) for c in components)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _cache_path(cache_type: str, key: str) -> str:
    return os.path.join(_cache_dir(cache_type), key + ".json")


def _atomic_write(path: str, data: Any) -> None:
    """Atomic JSON write."""
    import tempfile
    dir_name = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_name, exist_ok=True)
    content = json.dumps(data, indent=2, default=str)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def get_cached(cache_type: str, key: str) -> Optional[Any]:
    """Retrieve cached data if it exists and is valid."""
    path = _cache_path(cache_type, key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Validate cache version
        if data.get("_cache_version") != CACHE_VERSION:
            logger.info("Cache version mismatch for %s: %s != %s", key, data.get("_cache_version"), CACHE_VERSION)
            return None
        return data.get("payload")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def set_cached(cache_type: str, key: str, payload: Any, metadata: dict | None = None) -> None:
    """Store data in cache with metadata."""
    path = _cache_path(cache_type, key)
    data = {
        "_cache_version": CACHE_VERSION,
        "_cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_cache_type": cache_type,
        "_cache_key": key,
        "metadata": metadata or {},
        "payload": payload,
    }
    _atomic_write(path, data)


def invalidate_cache(cache_type: str, key: str) -> bool:
    """Invalidate a specific cache entry."""
    path = _cache_path(cache_type, key)
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False
    return False


def clear_cache(cache_type: str | None = None) -> int:
    """Clear cache entries. If cache_type is None, clear all."""
    count = 0
    if cache_type:
        dirs = [_cache_dir(cache_type)]
    else:
        if not os.path.exists(CACHE_BASE):
            return 0
        dirs = [os.path.join(CACHE_BASE, d) for d in os.listdir(CACHE_BASE)]
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if fname.endswith(".json"):
                try:
                    os.remove(os.path.join(d, fname))
                    count += 1
                except OSError:
                    pass
    return count


def cache_stats() -> dict:
    """Get cache statistics."""
    stats = {"total_entries": 0, "by_type": {}}
    if not os.path.exists(CACHE_BASE):
        return stats
    for ctype in os.listdir(CACHE_BASE):
        cdir = os.path.join(CACHE_BASE, ctype)
        if not os.path.isdir(cdir):
            continue
        count = len([f for f in os.listdir(cdir) if f.endswith(".json")])
        stats["by_type"][ctype] = count
        stats["total_entries"] += count
    return stats


# ---------------------------------------------------------------------------
# Cache key builders for each stage
# ---------------------------------------------------------------------------

def metadata_cache_key(source_hash: str, ffprobe_version: str = "unknown") -> str:
    return _cache_key("metadata", source_hash, ffprobe_version, CACHE_VERSION)


def transcript_cache_key(source_hash: str, engine: str, model: str, language: str, settings_hash: str) -> str:
    return _cache_key("transcript", source_hash, engine, model, language, settings_hash, CACHE_VERSION)


def audio_cache_key(source_hash: str, ffmpeg_version: str, sample_rate: int, window_size: float, algo_version: str) -> str:
    return _cache_key("audio", source_hash, ffmpeg_version, str(sample_rate), str(window_size), algo_version, CACHE_VERSION)


def visual_cache_key(source_hash: str, ffmpeg_version: str, frame_interval: float, algo_version: str) -> str:
    return _cache_key("visual", source_hash, ffmpeg_version, str(frame_interval), algo_version, CACHE_VERSION)


def candidate_cache_key(source_hash: str, transcript_key: str, audio_key: str, visual_key: str, candidate_version: str, config_hash: str) -> str:
    return _cache_key("candidates", source_hash, transcript_key, audio_key, visual_key, candidate_version, config_hash, CACHE_VERSION)


def evaluation_cache_key(candidate_key: str, eval_version: str, eval_config: str) -> str:
    return _cache_key("evaluation", candidate_key, eval_version, eval_config, CACHE_VERSION)


# ---------------------------------------------------------------------------
# Cached pipeline runner
# ---------------------------------------------------------------------------

def run_cached_pipeline(
    video_path: str,
    output_dir: str,
    transcript: list[dict] | None = None,
    use_cache: bool = True,
    progress_callback: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run the full analysis pipeline with caching.

    Returns dict with all results and timing information.
    """
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")
    import evaluation_engine as ee
    import deterministic_candidate as dc
    import signals as sig_mod

    result = {
        "video_path": video_path,
        "cache_hits": [],
        "cache_misses": [],
        "timing": {},
    }

    # Get metadata
    t0 = time.time()
    meta = ee.get_video_metadata(video_path)
    duration = meta["duration"] or 30.0
    source_hash = ee.compute_file_hash(video_path)
    result["metadata"] = meta
    result["source_hash"] = source_hash
    result["timing"]["metadata"] = round(time.time() - t0, 3)

    # Synthetic transcript if none provided
    if transcript is None:
        transcript = []
        seg_dur = 5.0
        for i in range(int(duration / seg_dur)):
            transcript.append({"start": i * seg_dur, "end": (i + 1) * seg_dur, "text": "Segment %d content." % (i + 1)})

    # Audio signals (cached)
    t0 = time.time()
    audio_key = audio_cache_key(source_hash, "ffmpeg", 16000, 1.0, "v1")
    audio_sigs = None
    if use_cache:
        audio_sigs = get_cached("audio", audio_key)
    if audio_sigs is not None:
        result["cache_hits"].append("audio")
    else:
        result["cache_misses"].append("audio")
        try:
            audio_sigs = sig_mod.extract_audio_signals(video_path, duration)
            if use_cache:
                set_cached("audio", audio_key, audio_sigs, {"source_hash": source_hash})
        except Exception as e:
            logger.warning("Audio extraction failed: %s", e)
            audio_sigs = {"loudness": [], "burst": [], "silence": []}
    result["timing"]["audio"] = round(time.time() - t0, 3)
    result["audio_signals"] = audio_sigs

    # Visual signals (cached)
    t0 = time.time()
    visual_key = visual_cache_key(source_hash, "ffmpeg", 1.0, "v1")
    visual_sigs = None
    if use_cache:
        visual_sigs = get_cached("visual", visual_key)
    if visual_sigs is not None:
        result["cache_hits"].append("visual")
    else:
        result["cache_misses"].append("visual")
        try:
            visual_sigs = sig_mod.extract_visual_signals(video_path, duration)
            if use_cache:
                set_cached("visual", visual_key, visual_sigs, {"source_hash": source_hash})
        except Exception as e:
            logger.warning("Visual extraction failed: %s", e)
            visual_sigs = {"motion": [], "scene_cut": []}
    result["timing"]["visual"] = round(time.time() - t0, 3)
    result["visual_signals"] = visual_sigs

    # Candidate generation (cached)
    t0 = time.time()
    cand_key = candidate_cache_key(source_hash, "transcript", audio_key, visual_key, "v1", "default")
    candidates = None
    if use_cache:
        candidates = get_cached("candidates", cand_key)
    if candidates is not None:
        result["cache_hits"].append("candidates")
    else:
        result["cache_misses"].append("candidates")
        candidates = dc.generate_candidates(transcript, duration, audio_sigs=audio_sigs, visual_sigs=visual_sigs)
        if use_cache:
            set_cached("candidates", cand_key, candidates, {"source_hash": source_hash, "candidate_count": len(candidates)})
    result["timing"]["candidates"] = round(time.time() - t0, 3)
    result["candidates"] = candidates
    result["candidate_count"] = len(candidates)

    # Evaluation
    t0 = time.time()
    words = []
    for seg in transcript:
        text = seg.get("text", "")
        seg_words = text.split()
        if not seg_words:
            continue
        per_word = (seg["end"] - seg["start"]) / len(seg_words)
        for i, w in enumerate(seg_words):
            words.append({"word": w, "start": seg["start"] + i * per_word, "end": seg["start"] + (i + 1) * per_word})

    report = ee.evaluate_video(candidates, transcript, duration, audio_sigs=audio_sigs, visual_sigs=visual_sigs, words=words, video_path=video_path, video_hash=source_hash, video_resolution="%dx%d" % (meta["width"], meta["height"]), video_fps=meta["fps"])
    result["timing"]["evaluation"] = round(time.time() - t0, 3)
    result["evaluation"] = report

    result["timing"]["total"] = round(sum(result["timing"].values()), 3)

    return result
