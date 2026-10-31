"""
audio_cleanup/publishing_gate.py — Publishing gate for audio cleanup.

Enforces that no video is marked publish-ready unless its audio
has passed verification (verified_music_free or verified_silent).

Integrates with the existing publishing system.
"""
from __future__ import annotations

import os
import logging
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# Statuses that allow publishing
PUBLISH_ALLOWED_STATUSES = {"verified_music_free", "verified_silent"}

# Statuses that block publishing
PUBLISH_BLOCKED_STATUSES = {
    "not_checked", "queued", "inspecting", "music_detected",
    "separating", "reconstructing", "verifying", "needs_review",
    "failed", "interrupted", "processing",
}


@dataclass
class PublishGateResult:
    """Result of a publishing gate check."""
    allowed: bool = False
    status: str = "not_checked"
    reason: str = ""
    video_id: str = ""
    output_hash: str = ""
    verification_timestamp: str = ""
    mode: str = ""


def check_publish_ready(video_id: str, output_hash: str | None = None) -> PublishGateResult:
    """Check if a video is ready for publishing based on audio cleanup status.

    Args:
        video_id: The video identifier.
        output_hash: Optional hash of the specific output file to check.

    Returns:
        PublishGateResult with allowed=True only if verification passed.
    """
    result = PublishGateResult(video_id=video_id)

    # Look for cleanup report
    report_dir = os.path.join("data", "audio_cleanup", "reports")
    report_path = os.path.join(report_dir, "%s_report.json" % video_id)

    if not os.path.exists(report_path):
        result.reason = "No audio cleanup report found. Run audio cleanup first."
        return result

    try:
        import json
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except Exception as e:
        result.reason = "Failed to read cleanup report: %s" % str(e)
        return result

    status = report.get("status", "not_checked")
    result.status = status
    result.mode = report.get("mode", "")
    result.output_hash = report.get("output_hash", "")
    result.verification_timestamp = report.get("verification_timestamp", "")

    # Check if output hash matches (if provided)
    if output_hash and result.output_hash and output_hash != result.output_hash:
        result.reason = "Output file has changed since cleanup. Re-run audio cleanup."
        return result

    if status in PUBLISH_ALLOWED_STATUSES:
        result.allowed = True
        result.reason = "Audio cleanup verified: %s" % status
    elif status in PUBLISH_BLOCKED_STATUSES:
        result.allowed = False
        result.reason = "Audio cleanup status '%s' does not allow publishing." % status
    else:
        result.allowed = False
        result.reason = "Unknown audio cleanup status: %s" % status

    return result


def save_cleanup_report(video_id: str, report: dict) -> str:
    """Save a cleanup report atomically."""
    import json, tempfile, os as _os
    report_dir = _os.path.join("data", "audio_cleanup", "reports")
    _os.makedirs(report_dir, exist_ok=True)
    report_path = _os.path.join(report_dir, "%s_report.json" % video_id)

    # Add timestamp
    report["verification_timestamp"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")

    # Atomic write
    content = json.dumps(report, indent=2, default=str)
    fd, tmp = tempfile.mkstemp(dir=report_dir, suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, report_path)
    except Exception:
        try:
            if _os.path.exists(tmp):
                _os.remove(tmp)
        except OSError:
            pass
        raise
    return report_path


def invalidate_cleanup_report(video_id: str) -> bool:
    """Invalidate a cleanup report (e.g., when video is modified)."""
    report_path = os.path.join("data", "audio_cleanup", "reports", "%s_report.json" % video_id)
    if os.path.exists(report_path):
        try:
            os.remove(report_path)
            return True
        except OSError:
            return False
    return False


def get_cleanup_status(video_id: str) -> dict:
    """Get the current cleanup status for a video."""
    report_path = os.path.join("data", "audio_cleanup", "reports", "%s_report.json" % video_id)
    if not os.path.exists(report_path):
        return {"status": "not_checked", "message": "No cleanup report found"}
    try:
        import json
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        return {
            "status": report.get("status", "not_checked"),
            "mode": report.get("mode", ""),
            "output_hash": report.get("output_hash", ""),
            "verification_timestamp": report.get("verification_timestamp", ""),
            "error": report.get("error", ""),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
