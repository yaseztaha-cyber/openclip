"""Audio cleanup module for music-free publishing."""
from .inspector import inspect_video, VideoAudioInspection, inspection_to_dict
from .processor import (
    process_mute_all, process_remove_music, process_review,
    get_separation_model_status, CleanupResult,
)
from .model_registry import check_model_availability, get_model_install_instructions

__all__ = [
    "inspect_video", "VideoAudioInspection", "inspection_to_dict",
    "process_mute_all", "process_remove_music", "process_review",
    "get_separation_model_status", "CleanupResult",
    "check_model_availability", "get_model_install_instructions",
]
