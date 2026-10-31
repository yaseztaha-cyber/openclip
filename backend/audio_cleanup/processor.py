"""
audio_cleanup/processor.py — Audio cleanup processing with three modes.

Modes:
1. remove_music: Attempt to remove background music while preserving speech
2. mute_all: Remove all audio streams (guaranteed music-free)
3. review: Inspect and report, no processing

Uses FFmpeg for audio processing. No external AI models required.
Source separation (Demucs/UVR) is not available in this environment.
"""
from __future__ import annotations

import subprocess
import os
import json
import logging
import time
import hashlib
from typing import Any, Optional, Callable
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Result of an audio cleanup operation."""
    job_id: str = ""
    video_id: str = ""
    source_path: str = ""
    output_path: str = ""
    source_hash: str = ""
    output_hash: str = ""
    mode: str = "remove_music"  # remove_music, mute_all, review
    status: str = "not_checked"  # not_checked, queued, inspecting, music_detected, processing, verifying, verified_music_free, verified_silent, needs_review, failed
    music_detected_before: bool = False
    music_detected_after: bool = False
    speech_preserved: bool = False
    original_duration: float = 0.0
    output_duration: float = 0.0
    duration_diff: float = 0.0
    av_sync_ok: bool = True
    video_preserved: bool = True
    processing_time: float = 0.0
    error: str = ""
    model_available: bool = False
    model_name: str = "none"
    verification_report: dict = field(default_factory=dict)


def get_separation_model_status() -> dict:
    """Check if any source separation model is available."""
    # Check for Demucs
    try:
        import demucs
        return {"available": True, "name": "demucs", "version": getattr(demucs, "__version__", "unknown")}
    except ImportError:
        pass
    # Check for UVR/MDX
    # No known UVR Python package
    return {"available": False, "name": "none", "version": "none",
            "message": "No source separation model installed. Music removal will use FFmpeg frequency filtering (limited effectiveness). Strict mute mode guarantees music-free output."}


def process_mute_all(video_path: str, output_path: str) -> CleanupResult:
    """Remove all audio streams from video. Guarantees music-free output."""
    result = CleanupResult(mode="mute_all", status="processing")
    t0 = time.time()

    # Use FFmpeg to copy video stream only, no audio
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-c:v", "copy",  # Preserve video stream
        "-an",  # No audio
        output_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            result.status = "failed"
            result.error = "FFmpeg failed: %s" % proc.stderr[:500]
            return result

        # Verify output has no audio
        verify_cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", output_path]
        verify_result = subprocess.run(verify_cmd, capture_output=True, text=True, timeout=30)
        output_streams = verify_result.stdout.strip().split("\n")
        has_audio = "audio" in output_streams

        if has_audio:
            result.status = "failed"
            result.error = "Output still contains audio streams"
            return result

        result.status = "verified_silent"
        result.output_path = output_path
        result.processing_time = time.time() - t0
        result.verification_report = {"audio_streams": 0, "verified": True, "mode": "mute_all"}

    except Exception as e:
        result.status = "failed"
        result.error = str(e)
        result.processing_time = time.time() - t0

    return result


def process_remove_music(video_path: str, output_path: str) -> CleanupResult:
    """Attempt to remove background music while preserving speech.

    Without a source separation model, uses FFmpeg frequency filtering
    which has limited effectiveness. Results should be verified.
    """
    result = CleanupResult(mode="remove_music", status="processing")
    t0 = time.time()

    model_status = get_separation_model_status()
    result.model_available = model_status["available"]
    result.model_name = model_status["name"]

    if model_status["available"]:
        # Use source separation model
        # (placeholder - would use demucs/UVR here)
        pass
    else:
        # Fallback: FFmpeg highpass filter to remove low-frequency music
        # This is NOT true source separation - limited effectiveness
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-c:v", "copy",  # Preserve video
            "-af", "highpass=f=200,lowpass=f=4000",  # Basic frequency filtering
            "-c:a", "aac", "-b:a", "128k",
            output_path,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                result.status = "failed"
                result.error = "FFmpeg failed: %s" % proc.stderr[:500]
                result.processing_time = time.time() - t0
                return result

            result.status = "needs_review"  # FFmpeg filtering is not reliable enough for verified status
            result.output_path = output_path
            result.processing_time = time.time() - t0
            result.verification_report = {
                "mode": "remove_music",
                "method": "ffmpeg_frequency_filter",
                "model_available": False,
                "note": "FFmpeg frequency filtering is not true source separation. Result requires manual review.",
            }

        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            result.processing_time = time.time() - t0

    return result


def process_review(video_path: str) -> CleanupResult:
    """Inspect audio and report without processing."""
    result = CleanupResult(mode="review", status="inspecting")
    t0 = time.time()

    from audio_cleanup.inspector import inspect_video
    inspection = inspect_video(video_path)

    result.original_duration = inspection.duration
    result.music_detected_before = inspection.music_detected
    result.speech_detected = inspection.speech_detected
    result.status = "needs_review"
    result.verification_report = {
        "mode": "review",
        "inspection": asdict(inspection),
        "note": "Review mode: no processing applied. User must choose a cleanup mode.",
    }
    result.processing_time = time.time() - t0

    return result
