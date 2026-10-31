"""
audio_cleanup/model_registry.py — Source separation model management.

Checks for available source separation models (Demucs, UVR, etc.)
and reports their status. No models are installed by default.

Available separation backends:
- None (no source separation model installed)
  → Strict mute mode provides guaranteed music-free output
  → FFmpeg frequency filtering provides limited (unverified) music reduction

Model requirements documented for user installation.
"""
from __future__ import annotations

import logging
import os
import hashlib
import subprocess
import json
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class ModelInfo:
    """Information about a source separation model."""
    name: str = ""
    available: bool = False
    version: str = ""
    license: str = ""
    size_mb: float = 0.0
    checksum: str = ""
    device: str = "cpu"
    python_version_required: str = ""
    torch_version_required: str = ""
    install_command: str = ""
    source_url: str = ""
    status: str = "not_installed"  # not_installed, downloading, installed, loading, ready, incompatible, failed
    error: str = ""
    capabilities: list = field(default_factory=list)
    note: str = ""

# Known source separation models
KNOWN_MODELS = {
    "demucs": {
        "name": "Demucs (htdemucs)",
        "license": "MIT",
        "size_mb": 80.0,
        "source_url": "https://github.com/facebookresearch/demucs",
        "install_command": "pip install demucs",
        "python_version_required": ">=3.8",
        "torch_version_required": ">=1.10",
        "capabilities": ["vocals", "drums", "bass", "other"],
        "note": "Primarily music source separation (vocals vs accompaniment). Not designed for cinematic dialogue/music/effects separation. Original repo archived.",
    },
    "bandit_v2": {
        "name": "Bandit v2 (Cinematic Separation)",
        "license": "CC BY-SA 4.0 (weights), MIT (reference implementation)",
        "size_mb": 200.0,
        "source_url": "https://github.com/kwatcharasupat/bandit-v2",
        "install_command": "See https://github.com/anaxoniclabs/ELUATE for reference implementation",
        "python_version_required": ">=3.9",
        "torch_version_required": ">=2.0",
        "capabilities": ["dialogue", "music", "effects"],
        "note": "Designed for cinematic audio source separation (dialogue/music/effects). Multilingual (30+ languages). Requires separate model download from Zenodo.",
    },
}


def check_model_availability() -> dict:
    """Check which source separation models are available."""
    result = {
        "any_available": False,
        "models": {},
        "recommended": None,
        "fallback": "strict_mute",
    }

    # Check Demucs
    demucs_info = ModelInfo(name="demucs", **{k: v for k, v in KNOWN_MODELS["demucs"].items() if k != "name"})
    try:
        import demucs
        demucs_info.available = True
        demucs_info.status = "ready"
        demucs_info.version = getattr(demucs, "__version__", "unknown")
        result["models"]["demucs"] = asdict(demucs_info)
        result["any_available"] = True
        if result["recommended"] is None:
            result["recommended"] = "demucs"
    except ImportError:
        demucs_info.status = "not_installed"
        result["models"]["demucs"] = asdict(demucs_info)

    # Check Bandit v2 (via torch hub or local checkpoint)
    bandit_info = ModelInfo(name="bandit_v2", **{k: v for k, v in KNOWN_MODELS["bandit_v2"].items() if k != "name"})
    # Check if checkpoint exists locally
    checkpoint_paths = [
        "models/bandit_v2/checkpoint.pt",
        "models/bandit_v2/model.pt",
    ]
    found = False
    for cp in checkpoint_paths:
        if os.path.exists(cp):
            found = True
            bandit_info.available = True
            bandit_info.status = "installed"
            # Compute checksum
            import hashlib
            h = hashlib.sha256()
            with open(cp, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            bandit_info.checksum = h.hexdigest()
            result["models"]["bandit_v2"] = asdict(bandit_info)
            result["any_available"] = True
            result["recommended"] = "bandit_v2"
            break
    if not found:
        bandit_info.status = "not_installed"
        result["models"]["bandit_v2"] = asdict(bandit_info)

    # Set fallback
    if not result["any_available"]:
        result["fallback"] = "strict_mute"
        result["recommendation"] = "No source separation model available. Use strict mute mode for guaranteed music-free output."
    else:
        result["recommendation"] = "Use %s for music removal. Strict mute mode available as fallback." % result["recommended"]

    return result


def get_model_install_instructions() -> dict:
    """Get instructions for installing source separation models."""
    return {
        "demucs": {
            "name": "Demucs (htdemucs)",
            "install": "pip install demucs",
            "note": "Music source separation (vocals vs accompaniment). Not cinematic dialogue/music/effects. Original repo archived.",
            "license": "MIT",
            "size": "~80MB",
        },
        "bandit_v2": {
            "name": "Bandit v2",
            "install": "Download checkpoint from https://zenodo.org/records/12701995 and place in models/bandit_v2/",
            "note": "Cinematic audio source separation (dialogue/music/effects). Requires manual download.",
            "license": "CC BY-SA 4.0 (weights)",
            "size": "~200MB",
        },
    }
