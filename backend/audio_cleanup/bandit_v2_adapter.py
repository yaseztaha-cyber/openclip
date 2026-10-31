"""
audio_cleanup/bandit_v2_adapter.py — Bandit v2 model adapter.

Communicates with the isolated audio_cleanup_worker venv to run
source separation inference. The worker runs bandit-infer with the
v2-multi checkpoint.

This adapter is designed for the existing OpenClip backend and
does not modify the application's Python environment.
"""
from __future__ import annotations

import json
import subprocess
import os
import logging
import tempfile
import time
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
WORKER_PYTHON = os.path.join(PROJECT_ROOT, "backend", "audio_cleanup_worker", "venv", "Scripts", "python.exe")
WORKER_SCRIPT = os.path.join(PROJECT_ROOT, "backend", "audio_cleanup_worker", "run_separation.py")
MODEL_DIR = os.path.join(PROJECT_ROOT, "data", "models", "audio_separation", "bandit_v2")
CHECKPOINT_NAME = "checkpoint-multi.ckpt"
EXPECTED_SHA256 = "abcfccf65446752a057f4a302c941479a54b7560ebf8d7bca039d2ea98e64cfc"


@dataclass
class SeparationResult:
    """Result of audio source separation."""
    success: bool = False
    speech_path: str = ""
    music_path: str = ""
    effects_path: str = ""
    sample_rate: int = 48000
    duration: float = 0.0
    channels: int = 2
    model_name: str = "bandit_v2"
    model_version: str = "v2-multi"
    processing_time: float = 0.0
    error: str = ""
    quality_metrics: dict = field(default_factory=dict)


def check_worker_available() -> dict:
    """Check if the audio worker environment is available."""
    result = {
        "worker_python_exists": os.path.exists(WORKER_PYTHON),
        "worker_script_exists": os.path.exists(WORKER_SCRIPT),
        "checkpoint_exists": os.path.exists(os.path.join(MODEL_DIR, CHECKPOINT_NAME)),
        "checkpoint_verified": False,
        "ready": False,
    }

    if result["checkpoint_exists"]:
        import hashlib
        h = hashlib.sha256()
        cp = os.path.join(MODEL_DIR, CHECKPOINT_NAME)
        with open(cp, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        result["checkpoint_verified"] = h.hexdigest() == EXPECTED_SHA256

    result["ready"] = (result["worker_python_exists"] and
                       result["worker_script_exists"] and
                       result["checkpoint_exists"] and
                       result["checkpoint_verified"])

    return result


def create_worker_script() -> str:
    """Create the worker script for audio separation."""
    script = '''import sys, json, os, time, numpy as np
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: run_separation.py <input_wav> <output_dir> <checkpoint_dir>"}))
        sys.exit(1)

    input_path = sys.argv[1]
    output_dir = sys.argv[2]
    checkpoint_dir = sys.argv[3]

    os.makedirs(output_dir, exist_ok=True)

    try:
        import soundfile as sf
    except ImportError:
        # Use scipy if soundfile not available
        from scipy.io import wavfile
        sf = None

    try:
        # Load audio
        if sf:
            audio, sr = sf.read(input_path, dtype="float32")
            if audio.ndim == 1:
                audio = audio[None, :]
            else:
                audio = audio.T  # (channels, samples)
        else:
            import numpy as np
            sr, audio = wavfile.read(input_path)
            audio = audio.astype(np.float32) / 32768.0
            if audio.ndim == 1:
                audio = audio[None, :]
            else:
                audio = audio.T

        if sr != 48000:
            # Resample using scipy
            from scipy.signal import resample
            new_len = int(len(audio[0]) * 48000 / sr)
            audio = np.stack([resample(ch, new_len) for ch in audio], axis=0)
            sr = 48000

        t0 = time.time()
        from bandit_infer import BanditSession
        session = BanditSession("v2-multi", device="cpu", weights_dir=Path(checkpoint_dir))
        session.load()
        result = session.infer(audio, sample_rate=sr)
        session.close()
        proc_time = time.time() - t0

        # Save stems
        speech_path = os.path.join(output_dir, "speech.wav")
        music_path = os.path.join(output_dir, "music.wav")
        effects_path = os.path.join(output_dir, "effects.wav")

        if sf:
            sf.write(speech_path, result["speech"].T, sr)
            sf.write(music_path, result["music"].T, sr)
            sf.write(effects_path, result["effects"].T, sr)
        else:
            from scipy.io import wavfile
            wavfile.write(speech_path, sr, (result["speech"].T * 32767).astype(np.int16))
            wavfile.write(music_path, sr, (result["music"].T * 32767).astype(np.int16))
            wavfile.write(effects_path, sr, (result["effects"].T * 32767).astype(np.int16))

        # Output metadata
        output = {
            "success": True,
            "speech_path": speech_path,
            "music_path": music_path,
            "effects_path": effects_path,
            "sample_rate": sr,
            "duration": len(audio[0]) / sr,
            "channels": audio.shape[0],
            "processing_time": round(proc_time, 2),
        }
        print(json.dumps(output))

    except Exception as e:
        import traceback
        output = {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }
        print(json.dumps(output))
        sys.exit(1)

if __name__ == "__main__":
    main()
'''
    return script


def run_separation(input_audio_path: str, output_dir: str, timeout: int = 600) -> SeparationResult:
    """Run audio separation using the isolated worker.

    Args:
        input_audio_path: Path to input audio file (WAV format preferred).
        output_dir: Directory for output stem files.
        timeout: Maximum processing time in seconds.

    Returns:
        SeparationResult with paths to separated stems.
    """
    result = SeparationResult()
    t0 = time.time()

    # Check worker availability
    status = check_worker_available()
    if not status["ready"]:
        result.error = "Worker not ready: %s" % status
        return result

    # Ensure worker script exists
    if not os.path.exists(WORKER_SCRIPT):
        os.makedirs(os.path.dirname(WORKER_SCRIPT), exist_ok=True)
        with open(WORKER_SCRIPT, "w", encoding="utf-8") as f:
            f.write(create_worker_script())

    try:
        proc = subprocess.run(
            [WORKER_PYTHON, WORKER_SCRIPT, input_audio_path, output_dir, MODEL_DIR],
            capture_output=True, text=True, timeout=timeout,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        )

        # Parse output
        stdout = proc.stdout.strip()
        if not stdout:
            result.error = "No output from worker: %s" % proc.stderr[:500]
            result.processing_time = time.time() - t0
            return result

        # Find JSON in output (last line)
        lines = stdout.split("\n")
        json_line = lines[-1]
        data = json.loads(json_line)

        if not data.get("success"):
            result.error = data.get("error", "Unknown error")
            result.processing_time = time.time() - t0
            return result

        result.success = True
        result.speech_path = data.get("speech_path", "")
        result.music_path = data.get("music_path", "")
        result.effects_path = data.get("effects_path", "")
        result.sample_rate = data.get("sample_rate", 48000)
        result.duration = data.get("duration", 0.0)
        result.channels = data.get("channels", 2)
        result.processing_time = data.get("processing_time", time.time() - t0)

        # Validate stems exist
        for path in [result.speech_path, result.music_path, result.effects_path]:
            if path and not os.path.exists(path):
                result.success = False
                result.error = "Stem file not found: %s" % path
                return result

    except subprocess.TimeoutExpired:
        result.error = "Processing timed out after %d seconds" % timeout
        result.processing_time = time.time() - t0
    except json.JSONDecodeError as e:
        result.error = "Failed to parse worker output: %s" % str(e)
        result.processing_time = time.time() - t0
    except Exception as e:
        result.error = "Separation failed: %s" % str(e)
        result.processing_time = time.time() - t0

    return result


def get_model_info() -> dict:
    """Get information about the Bandit v2 model."""
    status = check_worker_available()
    return {
        "model_name": "Bandit v2 (v2-multi)",
        "model_version": "v2-multi",
        "checkpoint": CHECKPOINT_NAME,
        "expected_sha256": EXPECTED_SHA256,
        "checkpoint_size_bytes": 446680129,
        "license": "CC-BY-SA-4.0 (weights)",
        "capabilities": ["speech", "music", "effects"],
        "sample_rate": 48000,
        "worker_status": status,
        "inference_tested": True,
        "inference_performance": "Model loads in ~182s. Inference on 0.01s audio exceeds 10min. NOT practical.",
        "cpu_feasible": False,
        "recommendation": "Strict mute mode is the practical fallback.",
    }
