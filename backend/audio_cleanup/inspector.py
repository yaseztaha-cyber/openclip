"""
audio_cleanup/inspector.py — Audio stream inspection using FFprobe.

Inspects video files for audio streams, their properties, and basic
audio characteristics. No external dependencies beyond FFmpeg/FFprobe.
"""
from __future__ import annotations

import json
import subprocess
import os
import logging
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class AudioStreamInfo:
    """Information about a single audio stream."""
    index: int = 0
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate: int = 0
    duration: float = 0.0
    channel_layout: str = ""


@dataclass
class VideoAudioInspection:
    """Complete audio inspection result for a video file."""
    file_path: str = ""
    file_hash: str = ""
    duration: float = 0.0
    video_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    audio_streams: list = field(default_factory=list)
    has_audio: bool = False
    audio_stream_count: int = 0
    # Analysis results
    music_likelihood: str = "unknown"  # music_only, speech_only, speech_plus_music, effects_only, mixed, no_audio, uncertain
    speech_detected: bool = False
    music_detected: bool = False
    silence_ratio: float = 0.0
    mean_volume: float = 0.0
    peak_volume: float = 0.0
    analysis_timestamp: str = ""
    error: str = ""


def inspect_video(video_path: str) -> VideoAudioInspection:
    """Inspect a video file's audio streams and basic properties."""
    result = VideoAudioInspection(file_path=video_path)

    if not os.path.exists(video_path):
        result.error = "File not found: %s" % video_path
        return result

    # Get video metadata
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,sample_rate,channels,bit_rate,channel_layout,duration",
             "-show_entries", "format=duration", "-of", "json", video_path],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            result.error = "ffprobe failed: %s" % probe.stderr[:200]
            return result

        data = json.loads(probe.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        # Video info
        for s in streams:
            if s.get("codec_type") == "video":
                result.video_codec = s.get("codec_name", "")
                result.width = s.get("width", 0)
                result.height = s.get("height", 0)
                fps_str = s.get("r_frame_rate", "0/1")
                if "/" in fps_str:
                    num, den = fps_str.split("/")
                    result.fps = float(num) / float(den) if float(den) > 0 else 0.0
                break

        result.duration = float(fmt.get("duration", 0))

        # Audio streams
        for s in streams:
            if s.get("codec_type") == "audio":
                info = AudioStreamInfo(
                    index=s.get("index", 0),
                    codec=s.get("codec_name", ""),
                    sample_rate=int(s.get("sample_rate", 0)),
                    channels=int(s.get("channels", 0)),
                    bitrate=int(s.get("bit_rate", 0)),
                    duration=float(s.get("duration", result.duration)),
                    channel_layout=s.get("channel_layout", ""),
                )
                result.audio_streams.append(info)

        result.has_audio = len(result.audio_streams) > 0
        result.audio_stream_count = len(result.audio_streams)

    except Exception as e:
        result.error = "Inspection failed: %s" % str(e)
        return result

    # Analyze audio content if audio exists
    if result.has_audio:
        _analyze_audio_content(video_path, result)

    return result


def _analyze_audio_content(video_path: str, result: VideoAudioInspection) -> None:
    """Analyze audio content for music/speech/silence characteristics."""
    import subprocess
    # Use FFmpeg to extract audio stats
    try:
        # Get volume stats
        stats_cmd = [
            "ffmpeg", "-i", video_path, "-af", "volumedetect", "-f", "null", "-"
        ]
        stats_result = subprocess.run(stats_cmd, capture_output=True, text=True, timeout=60)
        stderr = stats_result.stderr
        # Parse mean_volume and max_volume
        for line in stderr.split("\n"):
            if "mean_volume" in line:
                try:
                    val = line.split("mean_volume:")[1].strip().split()[0]
                    result.mean_volume = float(val)
                except (IndexError, ValueError):
                    pass
            if "max_volume" in line:
                try:
                    val = line.split("max_volume:")[1].strip().split()[0]
                    result.peak_volume = float(val)
                except (IndexError, ValueError):
                    pass

        # Get silence ratio
        silence_cmd = [
            "ffmpeg", "-i", video_path, "-af", "silencedetect=noise=-30dB:d=0.5", "-f", "null", "-"
        ]
        silence_result = subprocess.run(silence_cmd, capture_output=True, text=True, timeout=60)
        silence_stderr = silence_result.stderr
        # Count silence duration
        total_silence = 0.0
        for line in silence_stderr.split("\n"):
            if "silence_duration" in line:
                try:
                    val = line.split("silence_duration:")[1].strip().split()[0]
                    total_silence += float(val)
                except (IndexError, ValueError):
                    pass
        if result.duration > 0:
            result.silence_ratio = min(1.0, total_silence / result.duration)

        # Basic music/speech classification based on volume patterns
        # This is heuristic - true music detection requires more sophisticated analysis
        if result.silence_ratio > 0.8:
            result.music_likelihood = "no_audio" if result.peak_volume < -60 else "uncertain"
        elif result.peak_volume > -10 and result.silence_ratio < 0.3:
            # Loud with little silence - could be music or speech
            result.music_likelihood = "uncertain"
        else:
            result.music_likelihood = "uncertain"

        result.analysis_timestamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")

    except Exception as e:
        result.error = "Audio analysis failed: %s" % str(e)


def inspection_to_dict(insp: VideoAudioInspection) -> dict:
    """Convert inspection to serializable dict."""
    return asdict(insp)
