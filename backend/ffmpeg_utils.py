"""
ffmpeg_utils.py — Robust ffmpeg/ffprobe resolution.

The old code hardcoded:
    %LOCALAPPDATA%\\...\\ffmpeg-8.0.1-full_build\\bin\\ffmpeg.exe
which breaks as soon as Winget updates FFmpeg (e.g. to 9.0.1),
or when FFmpeg is installed via a different method.

Resolution order for each binary:
    1. Explicit env override (FFMPEG_PATH / FFPROBE_PATH / FFMPEG_DIR)
    2. shutil.which() (i.e. anything on PATH — Winget shims live here)
    3. Winget package dir glob (any Gyan.FFmpeg version, any build)
    4. Bundled backend/bin recursive search
    5. Common install locations
    6. Fallback to bare "ffmpeg" / "ffprobe" (let OS resolve, raise clear error on failure)
"""

import glob
import logging
import os
import shutil

logger = logging.getLogger(__name__)


def _winget_candidates(binary: str) -> list[str]:
    """Find Winget-installed FFmpeg binaries across any version."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    patterns = []
    if local_app:
        base = os.path.join(local_app, "Microsoft", "WinGet", "Packages")
        # Gyan.FFmpeg package id contains dots; match broadly
        patterns.append(os.path.join(base, "Gyan.FFmpeg*", "ffmpeg-*", "bin", binary))
        patterns.append(os.path.join(base, "Gyan.FFmpeg*", "bin", binary))
    # Also check Program Files locations
    patterns.append(os.path.join(r"C:\Program Files", "FFmpeg", "bin", binary))
    patterns.append(os.path.join(r"C:\Program Files (x86)", "FFmpeg", "bin", binary))

    found: list[str] = []
    for pat in patterns:
        try:
            found.extend(glob.glob(pat))
        except Exception:
            continue
    # Prefer highest version (sort descending so 9.x beats 8.x)
    found.sort(reverse=True)
    return [f for f in found if os.path.isfile(f)]


def _bundled_candidates(binary: str) -> list[str]:
    """Search backend/bin recursively for the binary."""
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    bin_dir = os.path.join(backend_dir, "bin")
    found: list[str] = []
    if os.path.isdir(bin_dir):
        for root, _dirs, files in os.walk(bin_dir):
            if binary.lower() in (f.lower() for f in files):
                # preserve actual casing
                for f in files:
                    if f.lower() == binary.lower():
                        found.append(os.path.join(root, f))
    return found


def resolve_binary(name: str, env_var: str) -> str:
    """
    Resolve `ffmpeg(.exe)` or `ffprobe(.exe)` to an executable path.

    Returns a path (or bare command name) suitable for subprocess.
    Never raises — falls back to `name` so subprocess raises a clear error.
    """
    # 1. Explicit env override
    override = os.environ.get(env_var, "")
    if override and os.path.isfile(override):
        return override

    # FFMPEG_DIR override (directory containing both binaries)
    ffmpeg_dir = os.environ.get("FFMPEG_DIR", "")
    if ffmpeg_dir:
        cand = os.path.join(ffmpeg_dir, name)
        if os.path.isfile(cand):
            return cand

    # 2. PATH
    on_path = shutil.which(name) or shutil.which(name.replace(".exe", ""))
    if on_path and os.path.isfile(on_path):
        return on_path

    # 3. Winget glob (any version)
    for cand in _winget_candidates(name):
        return cand
    # try without .exe on Windows too (shutil.which handles PATHEXT, glob doesn't)
    if name.lower().endswith(".exe"):
        for cand in _winget_candidates(name[:-4]):
            return cand

    # 4. Bundled bin/
    for cand in _bundled_candidates(name):
        return cand

    # 5. Fallback — let the OS try PATH at exec time
    logger.warning(
        "Could not locate %s; falling back to PATH lookup. "
        "Install FFmpeg (winget install Gyan.FFmpeg) or set %s.",
        name, env_var,
    )
    return name


def resolve_ffmpeg() -> str:
    """Path to ffmpeg executable."""
    # Accept ffmpeg.exe name on Windows, ffmpeg elsewhere
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    path = resolve_binary(name, "FFMPEG_PATH")
    # If we got bare "ffmpeg.exe" but we're on Windows, "ffmpeg" also works via PATHEXT
    return path


def resolve_ffprobe() -> str:
    """Path to ffprobe executable."""
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    return resolve_binary(name, "FFPROBE_PATH")


def resolve_ffmpeg_dir() -> str | None:
    """
    Directory containing ffmpeg (for yt-dlp --ffmpeg-location).
    Returns None if it cannot be determined — caller should omit the flag
    and let yt-dlp find ffmpeg on PATH.
    """
    ffmpeg = resolve_ffmpeg()
    if os.path.isfile(ffmpeg):
        return os.path.dirname(ffmpeg)
    # Try to infer from winget candidates even if resolve fell back
    cands = _winget_candidates("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if cands:
        return os.path.dirname(cands[0])
    return None


def ensure_on_path() -> None:
    """Prepend discovered ffmpeg dir + backend/bin to PATH for child processes."""
    paths_to_add: list[str] = []
    ffmpeg_dir = resolve_ffmpeg_dir()
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        paths_to_add.append(ffmpeg_dir)
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    for sub in ("bin",):
        d = os.path.join(backend_dir, sub)
        if os.path.isdir(d):
            paths_to_add.append(d)
        # also add nested ffmpeg build bin if present
        try:
            for entry in os.listdir(d):
                nested = os.path.join(d, entry, "bin")
                if os.path.isdir(nested):
                    paths_to_add.append(nested)
        except OSError:
            pass
    if not paths_to_add:
        return
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    for p in paths_to_add:
        if p not in parts:
            parts.insert(0, p)
    os.environ["PATH"] = os.pathsep.join(parts)
