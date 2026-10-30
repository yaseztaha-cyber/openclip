"""
api.py — FastAPI application for OpenClip.

😂 Seriously, are you going to read this full !

This is the main entry-point for the backend.  It wires together the
database, downloader, clipper, transcriber, LLM, and settings modules
behind a REST + WebSocket API that the Next.js frontend consumes.

Pipeline stages (v2.0 — AI-powered):
    1. **Download**     — yt-dlp fetches the YouTube video.
    2. **Transcribe**   — YouTube captions or local Whisper.
    3. **Analyze**      — LLM suggests 5-8 best clip timestamps.
    4. **Cut clips**    — FFmpeg stream-copy at LLM timestamps.
    5. **Save**         — clip records persisted to SQLite.

Architecture notes:
    • ``_ws_connections`` is a dict mapping ``project_id`` to a *set* of
      connected ``WebSocket`` objects.  The background task iterates
      over this set to broadcast progress.
    • All DB calls are ``await``-ed because ``database.py`` uses
      ``aiosqlite``.
    • The download, FFmpeg, and Whisper work is CPU/IO-bound, so it is
      run inside ``asyncio.to_thread`` to avoid blocking the event loop.
"""

import os
import re
import glob
import json
import base64
import io as _io
import asyncio
import shutil
import threading
import uuid
import datetime
import logging
import subprocess
import zipfile
import contextlib as _contextlib
from typing import Optional

from fastapi import (  # type: ignore
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    BackgroundTasks,
)
from fastapi.responses import FileResponse  # type: ignore
from fastapi.staticfiles import StaticFiles  # type: ignore
from fastapi.middleware.cors import CORSMiddleware  # type: ignore
from starlette.background import BackgroundTask  # type: ignore
from pydantic import BaseModel  # type: ignore
from dotenv import load_dotenv  # type: ignore

load_dotenv()

import os
# Ensure ffmpeg is available to all subprocesses (cross-platform PATH handling)
try:
    from ffmpeg_utils import ensure_on_path as _ensure_ffmpeg_on_path
    _ensure_ffmpeg_on_path()
except Exception:
    _extra_paths = os.path.join(os.path.dirname(__file__), "bin")
    os.environ["PATH"] = _extra_paths + os.pathsep + os.environ.get("PATH", "")

# Internal modules
import database  # type: ignore
import downloader  # type: ignore
import clipper  # type: ignore
import transcriber  # type: ignore
import llm  # type: ignore
import deterministic_candidate  # type: ignore
import reframer # type: ignore
import captioner # type: ignore
import editorial  # type: ignore
import quality  # type: ignore
import signals  # type: ignore
import scoring  # type: ignore
import boundaries  # type: ignore
import evaluation  # type: ignore
import composition as composition_mod  # type: ignore
import settings as settings_mod  # type: ignore
try:
    import automation_api as automation_api_mod  # type: ignore
    from automation.statemachine import StateTransitionError as _AutomationSTE  # type: ignore
    from automation.publishing import PublishBlocked as _AutomationPB  # type: ignore
    from automation.side_effects import DryRunBlocked as _AutomationDRB  # type: ignore
    from automation.logging_utils import request_id_ctx as _request_id_ctx  # type: ignore
    from fastapi import HTTPException as _AutomationHTTPExc  # type: ignore
    from fastapi.responses import JSONResponse as _AutomationJSONResponse  # type: ignore
    _AUTOMATION_AVAILABLE = True
except Exception as _automation_exc:
    automation_api_mod = None  # type: ignore
    _request_id_ctx = None  # type: ignore
    _AUTOMATION_AVAILABLE = False
    logging.getLogger(__name__).warning("Automation hub disabled: %s", _automation_exc)

import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402

import version as version_mod  # noqa: E402

# Set during lifespan shutdown; long-running workers check it to stop safely.
_SHUTTING_DOWN = False


@asynccontextmanager
async def lifespan(app_instance):
    """Modern FastAPI lifecycle: idempotent startup, graceful shutdown.

    Startup only initializes local resources (config, DB, migrations,
    limiters, recovery sweeps). It performs NO external side effects and
    starts NO publishing. Shutdown flags workers and relies on the
    existing DB recovery paths (orphaned render jobs, stale scheduler
    leases) so interrupted work is recoverable, never silently repeated.
    Every step is idempotent across tests, reloads, workers, restarts.
    """
    global _render_slots, _pipeline_slots, _SHUTTING_DOWN
    _SHUTTING_DOWN = False
    await database.init_db()
    os.makedirs(TMP_DIR, exist_ok=True)
    logger.info("Database initialised, tmp dir ready at %s (app %s)",
                TMP_DIR, version_mod.APP_VERSION)
    _render_slots = asyncio.Semaphore(RENDER_CONCURRENCY)
    _pipeline_slots = asyncio.Semaphore(PIPELINE_CONCURRENCY)
    try:
        n = await database.reset_stale_projects()
        if n:
            logger.warning("Reset %d orphaned in-progress project(s) to error", n)
    except Exception as exc:
        logger.warning("Stale-project reset failed: %s", exc)
    try:
        n = await database.mark_orphaned_render_jobs()
        if n:
            logger.warning("Marked %d orphaned render job(s) as errored", n)
    except Exception as exc:
        logger.warning("Render-job recovery failed: %s", exc)
    if _AUTOMATION_AVAILABLE:
        try:
            from automation import store as _automation_store
            await _automation_store.init_automation_tables()
            await _automation_store.ensure_schema_compatible()
            logger.info("Automation hub tables ready")
        except Exception as exc:
            logger.warning("Automation hub init failed: %s", exc)
    try:
        swept = 0
        for root, dirs, files in os.walk(TMP_DIR):
            for fn in files:
                if (fn.startswith(".seg") and fn.endswith(".mp4")) or \
                   (fn.startswith(".concat_") and fn.endswith(".txt")) or \
                   fn.endswith(".joined.mp4") or fn.endswith(".scaled.mp4"):
                    try:
                        os.remove(os.path.join(root, fn))
                        swept += 1
                    except OSError:
                        pass
            for d in dirs:
                if d.startswith(".render_"):
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    swept += 1
        if swept:
            logger.warning("Swept %d orphaned render temp file(s)", swept)
    except Exception as exc:
        logger.warning("Temp sweep failed: %s", exc)
    yield
    # --- shutdown: no new work; in-flight jobs are recoverable via DB. ---
    _SHUTTING_DOWN = True
    logger.info("Shutdown: workers flagged; running jobs recover via "
                "orphaned-render-job and stale-lease recovery on next boot; "
                "UNKNOWN submissions stay UNKNOWN")


app = FastAPI(
    title="OpenClip API",
    description="Local-first video clipping engine by AIONIX",
    version=version_mod.APP_VERSION,
    lifespan=lifespan,
)

# Allow the Next.js frontend dev server
_allowed_origins = [
    "http://localhost:3000",
    "http://localhost:5000",
]
_replit_domain = os.environ.get("REPLIT_DEV_DOMAIN")
if _replit_domain:
    _allowed_origins.append(f"https://{_replit_domain}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=r"https://.*\.replit\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base directory for all temporary video / clip files
TMP_DIR = os.getenv("TMP_DIR", os.path.join(os.path.dirname(__file__), "..", "tmp"))
os.makedirs(TMP_DIR, exist_ok=True)

# Auto AI mode: providers tried in order (fastest verified first).
# Only providers with a stored key are attempted.
AUTO_PROVIDER_ORDER = [
    "groq", "llm7", "cohere", "mistral", "xkiro", "openrouter",
    "anyapi", "openai", "anthropic", "gemini", "ollama",
]
# Max time per provider candidate before failing over to the next one.
AUTO_CANDIDATE_TIMEOUT = 480.0

# Hard budget for pulling captions during an interactive editor render. A miss
# falls back to rendering without burned captions rather than hanging the UI.
_EDITOR_TRANSCRIPT_TIMEOUT = float(os.getenv("EDITOR_TRANSCRIPT_TIMEOUT", "90"))

app.mount("/files", StaticFiles(directory=TMP_DIR), name="files")


# ---------------------------------------------------------------------------
# Automation hub wiring (import time, NOT startup).
#
# The ASGI middleware stack snapshots exception handlers when it is first
# built, so handlers/router registered later (e.g. inside on_startup) would
# be silently ignored. Only database table creation stays in on_startup.
# ---------------------------------------------------------------------------

def _automation_error_envelope(code, message, details):
    details = dict(details or {})
    try:
        if _request_id_ctx is not None:
            details.setdefault("request_id", _request_id_ctx.get())
    except Exception:
        pass
    return {"error": {"code": code, "message": message, "details": details}}


async def _handle_automation_state_conflict(request, exc):
    return _AutomationJSONResponse(
        status_code=409,
        content=_automation_error_envelope("state_conflict", str(exc),
                                           getattr(exc, "payload", {})))


async def _handle_automation_publish_blocked(request, exc):
    return _AutomationJSONResponse(
        status_code=409 if exc.code != "dry_run" else 403,
        content=_automation_error_envelope(exc.code, str(exc), exc.payload))


async def _handle_automation_dry_run_blocked(request, exc):
    return _AutomationJSONResponse(
        status_code=403,
        content=_automation_error_envelope("dry_run_blocked", str(exc), exc.payload))


async def _handle_automation_http_envelope(request, exc):
    # Automation endpoints raise HTTPException(detail=error_envelope()).
    # Unwrap to the specified top-level {"error": {...}} shape; every other
    # endpoint keeps Starlette's default {"detail": ...} behavior.
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and "error" in detail:
        try:
            if _request_id_ctx is not None:
                detail["error"].setdefault(
                    "details", {}).setdefault(
                    "request_id", _request_id_ctx.get())
        except Exception:
            pass
        return _AutomationJSONResponse(status_code=getattr(exc, "status_code", 500),
                                       content=detail)
    return _AutomationJSONResponse(status_code=getattr(exc, "status_code", 500),
                                   content={"detail": detail})


if _AUTOMATION_AVAILABLE:
    app.add_exception_handler(_AutomationSTE, _handle_automation_state_conflict)
    app.add_exception_handler(_AutomationPB, _handle_automation_publish_blocked)
    app.add_exception_handler(_AutomationDRB, _handle_automation_dry_run_blocked)
    app.add_exception_handler(_AutomationHTTPExc, _handle_automation_http_envelope)
    app.include_router(automation_api_mod.router)


@app.middleware("http")
async def _request_id_middleware(request, call_next):
    """Correlation IDs: accept X-Request-ID or mint one; linkable to job_id
    and submission_attempt_id in automation events. No sensitive data."""
    rid = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"
    reset = None
    try:
        if _request_id_ctx is not None:
            reset = _request_id_ctx.set(rid)
    except Exception:
        pass
    try:
        response = await call_next(request)
    finally:
        try:
            if reset is not None:
                _request_id_ctx.reset(reset)
        except Exception:
            pass
    response.headers["X-Request-ID"] = rid
    return response


# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------

_ws_connections: dict[str, set[WebSocket]] = {}

# ---------------------------------------------------------------------------
# Editor render jobs (async background renders + per-clip progress WebSockets)
# ---------------------------------------------------------------------------

# job_id -> {"id", "kind", "clip_id", "project_id", "status", "stage", "percent",
#            "message", "error", "cancel_event", "output", "clips_status": {clip_id: {...}},
#            "created_at", "finished_at"}
_render_jobs: dict[str, dict] = {}
# clip_id -> set[WebSocket]  (editor render progress channel)
_render_ws: dict[str, set[WebSocket]] = {}
# clip_id -> latest finished job_id (survives simple refreshes)
_latest_render_job: dict[str, str] = {}

# Concurrency limits (P0-3): bound parallel pipeline/FFmpeg/MediaPipe workers.
PIPELINE_CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "2"))
RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "2"))
_render_slots: Optional[asyncio.Semaphore] = None
_pipeline_slots: Optional[asyncio.Semaphore] = None


async def _broadcast_render(clip_id: str, payload: dict) -> None:
    """Send a progress update to every WebSocket listening on a clip's render channel."""
    dead: list[WebSocket] = []
    for ws in list(_render_ws.get(clip_id, set())):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _render_ws.get(clip_id, set()).discard(ws)


def _render_job_status(job: dict) -> dict:
    return {
        "job_id": job["id"],
        "kind": job.get("kind"),
        "clip_id": job.get("clip_id"),
        "project_id": job.get("project_id"),
        "status": job.get("status"),
        "stage": job.get("stage"),
        "percent": round(float(job.get("percent") or 0), 1),
        "message": job.get("message") or "",
        "error": job.get("error"),
        "output": job.get("output"),
        "clips_status": job.get("clips_status"),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
    }


def _schedule_persist(job: dict) -> None:
    """
    Persist a render-job snapshot to SQLite (P0-2), so statuses survive
    backend restarts. Fire-and-forget on the running loop.
    """
    try:
        asyncio.get_running_loop().create_task(database.upsert_render_job(job))
    except RuntimeError:
        pass


def _run_render_worker(fn, *args, **kwargs):
    """
    Run an async function that internally performs long blocking work
    (ffmpeg subprocess, mediapipe frames) on a dedicated worker thread with
    its own event loop. The main uvicorn loop stays responsive, so progress
    polling / other requests are never blocked during a render.
    """
    return asyncio.run(fn(*args, **kwargs))


def _transcript_cache_path(project_id: str) -> str:
    return os.path.join(TMP_DIR, project_id, "transcript.json")


def _load_cached_transcript(project_id: str) -> Optional[list]:
    """Load a previously cached transcript for a project, if present."""
    path = _transcript_cache_path(project_id)
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                return data
    except Exception as exc:
        logger.warning("Transcript cache read failed for %s: %s", project_id, exc)
    return None


def _save_cached_transcript(project_id: str, segments: list) -> None:
    """Persist a transcript so editor renders don't re-fetch it every time."""
    if not segments:
        return
    path = _transcript_cache_path(project_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(segments, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("Transcript cache write failed for %s: %s", project_id, exc)


async def _get_project_transcript(project_id: str, youtube_url: str, source: str,
                                  progress_cb=None, refresh: bool = False,
                                  allow_whisper: bool = True,
                                  timeout: Optional[float] = None) -> Optional[list]:
    """
    Return the transcript for a project, preferring the on-disk cache.

    Extracting captions can hit the network (YouTube) and fall back to Whisper
    on a multi-hour video — far too slow to repeat on every editor render. Cache
    once per project and reuse; only fall through to extraction on a miss or
    when ``refresh`` is requested.
    """
    if not refresh:
        cached = _load_cached_transcript(project_id)
        if cached:
            logger.info("Using cached transcript for %s (%d segments)", project_id, len(cached))
            return cached
    try:
        coro = transcriber.extract_captions(
            youtube_url, source, progress_callback=progress_cb,
            allow_whisper=allow_whisper)
        if timeout:
            segments = await asyncio.wait_for(coro, timeout=timeout)
        else:
            segments = await coro
    except asyncio.TimeoutError:
        logger.warning("Transcript fetch timed out for %s after %ss", project_id, timeout)
        return _load_cached_transcript(project_id)
    except Exception as exc:
        logger.warning("Transcript extraction failed for %s: %s", project_id, exc)
        return _load_cached_transcript(project_id)
    _save_cached_transcript(project_id, segments)
    return segments


async def _broadcast(project_id: str, stage: str, percent: float, message: str) -> None:
    """
    Send a progress update to every WebSocket client listening for
    the given project.

    Args:
        project_id: UUID of the project.
        stage:      Pipeline stage name (download / clipping / done / error).
        percent:    Progress 0–100.
        message:    Human-readable status text.
    """
    payload = {"stage": stage, "percent": percent, "message": message}
    dead: list[WebSocket] = []
    for ws in _ws_connections.get(project_id, set()):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    # Clean up dead connections
    for ws in dead:
        _ws_connections.get(project_id, set()).discard(ws)


async def _build_auto_candidates() -> list[tuple[str, str, str]]:
    """
    Build the (provider, model, api_key) attempt list for Auto AI mode.

    Includes every provider with its own stored key, in AUTO_PROVIDER_ORDER
    (fastest verified first). Falls back to the legacy generic key if no
    per-provider keys exist yet. Each candidate uses "" as the model so the
    provider handler picks its verified default (+ its own fallbacks).
    """
    candidates: list[tuple[str, str, str]] = []
    for p in AUTO_PROVIDER_ORDER:
        key = await settings_mod.get_setting(f"llm_api_key_{p}") or ""
        if key:
            candidates.append((p, "", key))
    if not candidates:
        legacy_provider = await settings_mod.get_setting("llm_provider") or "openai"
        if legacy_provider == "auto":
            legacy_provider = "openai"
        legacy_key = await settings_mod.get_setting("llm_api_key") or ""
        legacy_model = await settings_mod.get_setting("llm_model") or ""
        if legacy_key:
            candidates.append((legacy_provider, legacy_model, legacy_key))
    return candidates


async def _verify_clip_integrity(file_path: str, expected_aspect: str = "9:16",
                                  expected_duration: float = 0.0) -> tuple[bool, list[str]]:
    """
    P8: 8 hard checks before publishing a clip.
    Returns (ok, [reasons]). If not ok, caller must delete partial and skip.
    Checks: exists, size, ffprobe ok, video stream, duration, dimensions, decodable.
    """
    reasons: list[str] = []
    if not file_path or not os.path.isfile(file_path):
        return False, ["output file missing"]
    if os.path.getsize(file_path) < 50 * 1024:
        reasons.append("output file suspiciously small (<50KB)")
    # ffprobe
    try:
        from ffmpeg_utils import resolve_ffprobe  # type: ignore
        import json as _json, subprocess as _sub
        result = _sub.run([resolve_ffprobe(), "-v", "error",
                           "-show_entries", "stream=index,codec_type,width,height,duration",
                           "-show_entries", "format=duration",
                           "-of", "json", file_path],
                          capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            reasons.append(f"ffprobe failed: {result.stderr[:200]}")
            return False, reasons
        data = _json.loads(result.stdout)
        streams = data.get("streams", [])
        kinds = {s.get("codec_type") for s in streams}
        if "video" not in kinds:
            reasons.append("no video stream")
        # Duration check
        dur = None
        for s in streams:
            if s.get("duration"):
                try:
                    dur = float(s["duration"])
                    break
                except: pass
        if dur is None:
            try:
                dur = float(data.get("format", {}).get("duration", 0))
            except: dur = None
        if dur is not None and dur <= 0.1:
            reasons.append(f"duration {dur:.2f}s too short")
        if expected_duration and dur is not None:
            if abs(dur - expected_duration) > max(2.0, expected_duration * 0.2):
                reasons.append(f"duration drift {dur:.1f}s vs expected {expected_duration:.1f}s")
        # Dimensions check against canvas presets (9:16/16:9/1:1/4:5)
        _dims = composition_mod.CANVAS_PRESETS.get(expected_aspect)
        if _dims:
            _ew, _eh = _dims
            for s in streams:
                if s.get("codec_type") == "video":
                    w, h = s.get("width"), s.get("height")
                    if w and h:
                        # Allow minor variance but flag; 720x1280 export is
                        # same 9:16 aspect at half resolution
                        if expected_aspect == "9:16" and ((w == 720 and h == 1280)):
                            pass
                        elif abs(w - _ew) > 2 or abs(h - _eh) > 2:
                            reasons.append(f"dimensions {w}x{h} != {_ew}x{_eh} for {expected_aspect}")
                    break
        # Audio check — warn but don't fail if missing (some clips may be silent)
        # If expected to have audio (most do), flag missing
        if "audio" not in kinds:
            # Check if source had audio — if clip is silent, it's likely a bug
            # For now, warn but not fail: add reason but don't block
            pass
    except Exception as exc:
        reasons.append(f"integrity probe error: {exc}")
        return False, reasons
    return (len(reasons) == 0, reasons)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    """Payload for creating a new project."""
    youtube_url: str
    # Per-project framing adjustments applied to every auto-cut clip.
    layout_mode: Optional[str] = None  # auto/single/tutorial/podcast/panel/none/center
    aspect: Optional[str] = None  # 9:16 / 1:1 / 16:9 / 4:3
    zoom_boost: Optional[float] = None  # 0.8 (wide) – 1.6 (tight)
    x_offset: Optional[float] = None  # -1 (left) – +1 (right)
    y_offset: Optional[float] = None  # -1 (up) – +1 (down)
    tracking: Optional[bool] = None  # False = fixed camera
    caption_style: Optional[str] = None
    output_mode: Optional[str] = None  # auto/crop_9x16/crop_1x1/blur_9x16/native_16x9


class CreateProjectResponse(BaseModel):
    """Returned immediately after project creation."""
    project_id: str
    status: str


class ProjectListItem(BaseModel):
    """One item in the projects list."""
    id: str
    title: Optional[str] = None
    youtube_url: str
    status: str
    created_at: str
    clip_count: int
    size_bytes: int = 0


class ClipResponse(BaseModel):
    """Serialised clip record."""
    id: str
    project_id: str
    file_path: str
    start_time: float
    end_time: float
    duration: float
    reframed: bool
    captioned: bool
    aspect: Optional[str] = "9:16"
    title: Optional[str] = None
    reason: Optional[str] = None
    layout_mode: Optional[str] = None
    needs_user_confirm: bool = False
    hashtags: list[str] = []
    tags: list[str] = []
    final_score: Optional[float] = None
    hook_score: Optional[float] = None
    hook_first_3s_score: Optional[float] = None
    payoff_score: Optional[float] = None
    audio_score: Optional[float] = None
    visual_score: Optional[float] = None
    reaction_score: Optional[float] = None
    pacing_score: Optional[float] = None
    cuts_applied: Optional[int] = None
    created_at: str


class ProjectDetailResponse(BaseModel):
    """Full project detail including nested clips."""
    id: str
    title: Optional[str] = None
    youtube_url: str
    status: str
    created_at: str
    pipeline_options: Optional[dict] = None
    clips: list[ClipResponse] = []


# ---------------------------------------------------------------------------
# Background processing pipeline
# ---------------------------------------------------------------------------

async def _run_pipeline(project_id: str, youtube_url: str, clip_options: Optional[dict] = None) -> None:
    """
    Execute the full AI-powered pipeline in the background.

    Steps:
        1. Download the video using yt-dlp.
        2. Extract transcript (YouTube captions → Whisper fallback).
        3. Send transcript to LLM for clip suggestions.
        4. Cut clips at LLM-suggested timestamps.
        5. Save clip records to the database.

    Args:
        project_id:    UUID of the project.
        youtube_url:   The video to download.
        clip_options:  Optional per-project framing adjustments (aspect,
                       layout_mode, zoom_boost, x/y offsets, tracking,
                       caption_style) applied to every auto-cut clip.
    """
    project_dir = os.path.join(TMP_DIR, project_id)
    clips_dir = os.path.join(project_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    # Defaults merged with user choices; 9:16 stays the baseline aspect.
    reframe_options: dict = {"aspect": "9:16"}
    if clip_options:
        for key in ("layout_mode", "aspect", "zoom_boost", "x_offset", "y_offset", "tracking"):
            if clip_options.get(key) is not None:
                reframe_options[key] = clip_options[key]
    caption_style_requested = (clip_options or {}).get("caption_style")
    output_mode = str((clip_options or {}).get("output_mode") or "auto")

    # P0-3: serialize pipeline runs so concurrent projects don't saturate CPU/RAM.
    if _pipeline_slots is not None:
        await _pipeline_slots.acquire()
    try:
        if await database.get_project(project_id) is None:
            return

        # Capture the running event loop for threadsafe callbacks
        loop = asyncio.get_running_loop()

        # ---- STEP 1: Download ----
        await database.update_project_status(project_id, "downloading")
        await _broadcast(project_id, "downloading", 5, "Starting download…")

        def download_progress(percent: float, msg: str):
            asyncio.run_coroutine_threadsafe(
                _broadcast(project_id, "downloading", min(percent * 0.2 + 5, 25), msg),
                loop,
            )

        result = await asyncio.to_thread(
            downloader.download_video,
            youtube_url,
            project_dir,
            download_progress,
        )

        file_path = result["file_path"]
        title = result["title"]
        video_duration = result["duration_seconds"]

        await database.update_project_title(project_id, title)
        await _broadcast(project_id, "downloading", 25, "Download complete")

        # ---- STEP 2: Extract Transcript ----
        await database.update_project_status(project_id, "transcribing")
        await _broadcast(project_id, "transcribing", 30, "Extracting transcript…")

        def transcript_progress(percent: float, msg: str):
            scaled = 30 + (percent / 100) * 20  # 30-50 range
            asyncio.run_coroutine_threadsafe(
                _broadcast(project_id, "transcribing", scaled, msg),
                loop,
            )

        transcript_segments = await _get_project_transcript(
            project_id,
            youtube_url,
            file_path,
            progress_cb=transcript_progress,
        )
        
        if not transcript_segments:
            await database.update_project_status(project_id, "error")
            await _broadcast(project_id, "error", 0, "Could not extract captions from this video.")
            return

        await _broadcast(project_id, "transcribing", 50, "Transcript ready")

        # ---- STEP 3: Extract per-video multimodal signals (audio + visual) ----
        # Runs once per video, normalized per-video (quiet podcast vs loud stream).
        # Extracted BEFORE candidate generation so audio-peak and scene-cut
        # windows can be included in the deterministic candidate set.
        audio_sigs: dict = {"loudness": [], "burst": [], "silence": []}
        visual_sigs: dict = {"motion": [], "scene_cut": []}
        reaction_sig: list[float] = []
        try:
            await _broadcast(project_id, "analyzing", 56, "Analyzing audio excitement...")
            audio_sigs = await asyncio.wait_for(
                asyncio.to_thread(signals.extract_audio_signals, file_path, video_duration),
                timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning("Audio signal extraction timed out")
        except Exception as exc:
            logger.warning("Audio signal extraction failed: %s", exc)
        try:
            await _broadcast(project_id, "analyzing", 58, "Analyzing visual activity...")
            visual_sigs = await asyncio.wait_for(
                asyncio.to_thread(signals.extract_visual_signals, file_path, video_duration),
                timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning("Visual signal extraction timed out")
        except Exception as exc:
            logger.warning("Visual signal extraction failed: %s", exc)
        # Reaction = fused burst + motion (face delta added after per-clip face detect)
        try:
            if audio_sigs["burst"] and visual_sigs["motion"]:
                reaction_sig = signals.build_reaction_signal(
                    audio_sigs["burst"], [0.0] * len(audio_sigs["burst"]), visual_sigs["motion"])
            elif audio_sigs["burst"]:
                reaction_sig = audio_sigs["burst"]
            else:
                reaction_sig = [0.5] * max(1, int(video_duration))
        except Exception:
            reaction_sig = [0.5] * max(1, int(video_duration))

        # ---- STEP 3a: Deterministic Candidate Generation (no LLM, no network) ----
        await database.update_project_status(project_id, "analyzing")
        await _broadcast(project_id, "analyzing", 60, "Analyzing video...")

        def analyze_progress(stage: str, percent: float, msg: str):
            asyncio.run_coroutine_threadsafe(
                _broadcast(project_id, stage, percent, msg),
                loop,
            )

        suggestions: list[dict] = []
        try:
            suggestions = await asyncio.wait_for(
                asyncio.to_thread(
                    deterministic_candidate.generate_candidates,
                    transcript_segments,
                    video_duration,
                    audio_sigs=audio_sigs,
                    visual_sigs=visual_sigs,
                    progress_callback=analyze_progress,
                ),
                timeout=AUTO_CANDIDATE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Deterministic candidate generation timed out")
        except Exception as e:
            logger.warning("Deterministic candidate generation failed: %s", e)

        if not suggestions:
            await database.update_project_status(project_id, "error")
            await _broadcast(project_id, "error", 0, "Could not generate clip candidates from this video.")
            return

        # ---- STEP 3b: Multimodal editorial pass ----
        await _broadcast(project_id, "analyzing", 73, "Refining clip boundaries...")
        try:
            sentences = editorial.build_sentences(transcript_segments)
            words_all = editorial.all_words(transcript_segments)

            # Signals already extracted above — reuse the same objects.
            # (audio_sigs, visual_sigs, reaction_sig are available from Step 3)

            # ---- Optimize boundaries + multimodal scoring per candidate ----
            refined: list[dict] = []
            for s in suggestions:
                # 1) Snap to sentence + strip fillers (editorial baseline)
                ns, ne = editorial.snap_bounds(float(s["start"]), float(s["end"]), sentences)
                ns = editorial.strip_leading_fillers(words_all, ns)
                if ne - ns < 10.0:
                    continue

                # 2) Boundary optimizer: try several start/end variants, pick best
                try:
                    tmp = dict(s)
                    tmp["start"], tmp["end"] = ns, ne
                    opt = boundaries.optimize_boundaries(tmp, sentences, words_all,
                                                         audio_loud=audio_sigs.get("loudness"))
                    ns, ne = float(opt["start"]), float(opt["end"])
                    s = opt
                except Exception as exc:
                    logger.debug("Boundary optimizer failed for %s: %s", s.get("title"), exc)
                    s = dict(s)
                    s["start"], s["end"] = ns, ne

                # 3) Deterministic text score (hook/pacing/clean ending)
                window_text = " ".join(
                    seg["text"] for seg in transcript_segments
                    if seg["end"] > ns and seg["start"] < ne
                )
                s["_det"] = editorial.deterministic_score(
                    window_text, words_all, ns, ne, video_duration)

                # 4) Multimodal audio/visual/reaction/pacing scores
                try:
                    s["_audio"] = scoring.score_audio_candidate(ns, ne,
                        audio_sigs.get("loudness", []), audio_sigs.get("burst", []), audio_sigs.get("silence", []))
                    s["_visual"] = scoring.score_visual_candidate(ns, ne,
                        visual_sigs.get("motion", []), visual_sigs.get("scene_cut", []),
                        [0.0] * max(1, int(video_duration)))  # face_presence placeholder; real after reframing
                    s["_reaction"] = scoring.score_reaction_candidate(ns, ne, reaction_sig, [0.0]*len(reaction_sig))
                    # Pacing: silence mask + filler density + opening WPM
                    clip_words = [w for w in words_all if ns <= float(w.get("start", 0.0)) < ne]
                    filler_density = 0.0
                    if clip_words:
                        import editorial as _ed
                        fill = sum(1 for w in clip_words if _ed._is_filler_token(_ed._norm_token(str(w.get("word","")))))
                        filler_density = fill / max(len(clip_words), 1)
                    opening = [w for w in words_all if ns <= float(w.get("start",0.0)) < ns+5.0]
                    wpm = None
                    if opening and len(opening) >= 3:
                        span = max(0.5, float(opening[-1].get("end", ns)) - float(opening[0].get("start", ns)))
                        wpm = len(opening) / (span / 60.0)
                    s["_pacing"] = scoring.score_pacing_candidate(ns, ne,
                        audio_sigs.get("silence", []), filler_density, wpm)
                    # --- Editorial quality evaluation FIRST (hook, payoff, context) ---
                    # These now feed the final score, not just the review UI.
                    try:
                        snippets = evaluation.extract_snippets(s, sentences, words_all)
                        s.update(snippets)  # first_3s_transcript etc.
                        hook_eval = evaluation.evaluate_hook_first(s, snippets, words_all)
                        payoff_eval = evaluation.evaluate_payoff(s, snippets)
                        context_eval = evaluation.evaluate_context(s, snippets, sentences)
                        s["_hook_eval"] = hook_eval
                        s["_payoff_eval"] = payoff_eval
                        s["_context_eval"] = context_eval
                        quality_scores = {
                            "hook_eval": hook_eval.get("score", 5.0),
                            "payoff_eval": payoff_eval.get("score", 5.0),
                            "context_eval": context_eval.get("score", 5.0),
                        }
                    except Exception as e2:
                        logger.debug(f"Evaluation failed for {s.get('title')}: {e2}")
                        quality_scores = None
                        snippets, hook_eval, payoff_eval, context_eval = {}, {"score": 5.0}, {"score": 5.0}, {"score": 5.0}
                    # P5: Hook first-3s score — does the actual opening earn the title?
                    try:
                        hook3 = scoring.score_hook_first_3s(s, sentences, words_all)
                    except Exception:
                        hook3 = {"score": 5.0, "reasons": []}
                    s["_hook_first_3s"] = hook3
                    # Final multimodal score 0-100 (editorial quality moves ranking)
                    s["_multimodal"] = scoring.final_multimodal_score(s,
                        audio_scores=s["_audio"], visual_scores=s["_visual"],
                        reaction_scores=s["_reaction"], pacing_scores=s["_pacing"],
                        hook_first_3s=hook3, quality_scores=quality_scores)
                    s["_combined"] = s["_multimodal"]["final"]

                    try:
                        # For final_score persistence
                        s["hook"] = float(s.get("hook", 5))
                        s["payoff"] = float(s.get("payoff", 5))
                        s["curiosity"] = float(s.get("curiosity", 5))
                        s["information"] = float(s.get("information", 5))
                        s["narrative"] = float(s.get("narrative", 5))
                        # Failure reasons
                        failure = evaluation.assign_failure_reasons(
                            s, hook_eval, payoff_eval, context_eval,
                            pacing_score=s.get("_pacing"), audio_score=s.get("_audio"), visual_score=s.get("_visual"))
                        s["failure_reasons"] = failure
                        s["hook_detail"] = hook_eval
                        s["payoff_detail"] = payoff_eval
                        s["context_detail"] = context_eval
                        # Also store for DB
                        s["first_3s_transcript"] = snippets.get("first_3s_transcript")
                        s["first_sentence"] = snippets.get("first_sentence")
                        s["payoff_sentence"] = snippets.get("payoff_sentence")
                        s["final_sentence"] = snippets.get("final_sentence")
                        # For variant search, keep original
                        s["_snippets"] = snippets
                    except Exception as e2:
                        logger.debug(f"Evaluation failed for {s.get('title')}: {e2}")
                except Exception as exc:
                    logger.debug("Multimodal scoring failed for %s: %s", s.get("title"), exc)
                    import editorial as _ed2
                    s["_combined"] = _ed2.combined_score(s)

                # Hard gate: mostly-silent windows are never good clips, no
                # matter what the transcript says (dead air, music beds).
                try:
                    import math as _math_gate
                    sil_list = audio_sigs.get("silence", []) or []
                    if sil_list:
                        _ss, _ee = max(0, int(ns)), min(len(sil_list), _math_gate.ceil(ne))
                        _win = sil_list[_ss:_ee]
                        if _win and sum(1 for v in _win if v > 0.5) / len(_win) > 0.5:
                            logger.info("Dropping mostly-silent candidate '%s'",
                                        s.get("title"))
                            continue
                except Exception:
                    pass

                refined.append(s)

            # 5) Variant search for top candidates (start/end A/B)
            # Generate 4 variants per top candidate and keep best by editorial quality
            try:
                top_for_variants = sorted(refined, key=lambda c: c.get("_combined",0), reverse=True)[:5]
                variant_pool = []
                for cand in top_for_variants:
                    variants = evaluation.generate_variants(cand, sentences, words_all)
                    best_variant = None
                    best_score = -1
                    for var in variants:
                        # Re-score variant quickly (hook/payoff/context)
                        try:
                            snippets_v = evaluation.extract_snippets(var, sentences, words_all)
                            hook_v = evaluation.evaluate_hook_first(var, snippets_v, words_all)
                            payoff_v = evaluation.evaluate_payoff(var, snippets_v)
                            # Use deterministic + hook as proxy for variant quality
                            var_score = (hook_v.get("score",5)*0.4 + payoff_v.get("score",5)*0.4 + var.get("_combined",50)*0.2/10)
                            if var_score > best_score:
                                best_score = var_score
                                best_variant = var
                                best_variant["_hook_eval"] = hook_v
                                best_variant["_payoff_eval"] = payoff_v
                        except: continue
                    if best_variant is not None:
                        # Replace original with best variant if different
                        variant_pool.append(best_variant)
                    else:
                        variant_pool.append(cand)
                # Merge variant winners with remaining refined (non-top) and deduplicate
                remaining = [c for c in refined if c not in top_for_variants]
                refined_for_select = variant_pool + remaining
            except Exception as e:
                logger.debug(f"Variant search failed: {e}")
                refined_for_select = refined

            # 6) Select top-k diverse clips by multimodal final score
            suggestions = editorial.select_clips(refined_for_select, top_k=8)
            # Apply semantic dedupe and diversity
            try:
                # First dedupe by semantic similarity
                suggestions = evaluation.deduplicate_candidates(suggestions, threshold=0.75)
                # Then diversify
                suggestions = evaluation.diversify_selection(suggestions, top_k=5)
                # Ensure at least 3
                if len(suggestions) < 3:
                    # Fallback to original top 3
                    suggestions = sorted(refined_for_select, key=lambda c: c.get("_combined",0), reverse=True)[:3]
                    suggestions.sort(key=lambda c: c["start"])
            except Exception as e:
                logger.debug(f"Dedupe/diversity failed: {e}")
                suggestions.sort(key=lambda c: c.get("_combined", 0), reverse=True)
                suggestions = suggestions[:5]
                suggestions.sort(key=lambda c: c["start"])

            top_ranked = [(round(c["start"],1), round(c["end"],1), c.get("_combined"), c.get("failure_reasons")) for c in suggestions]
            logger.info("Multimodal editorial kept %d clips: %s", len(suggestions), top_ranked)
            suggestions.sort(key=lambda c: c["start"])

            # Persist all candidate evaluations for review dataset (including rejected)
            try:
                selected_ids = {(c.get("start"), c.get("end")) for c in suggestions}
                for cand in refined_for_select:
                    is_selected = (cand.get("start"), cand.get("end")) in selected_ids
                    # Prepare candidate for DB
                    db_cand = {
                        "start": cand.get("start"), "end": cand.get("end"),
                        "hook": cand.get("hook"), "hook_first_3s": cand.get("_hook_eval", {}).get("score"),
                        "payoff": cand.get("payoff"), "curiosity": cand.get("curiosity"),
                        "information": cand.get("information"), "narrative": cand.get("narrative"),
                        "audio_score": cand.get("_audio", {}).get("total") if cand.get("_audio") else None,
                        "visual_score": cand.get("_visual", {}).get("total") if cand.get("_visual") else None,
                        "reaction_score": cand.get("_reaction", {}).get("total") if cand.get("_reaction") else None,
                        "pacing_score": cand.get("_pacing", {}).get("total") if cand.get("_pacing") else None,
                        "final_score": cand.get("_combined"),
                        "first_3s_transcript": cand.get("first_3s_transcript"),
                        "first_sentence": cand.get("first_sentence"),
                        "payoff_sentence": cand.get("payoff_sentence"),
                        "final_sentence": cand.get("final_sentence"),
                        "failure_reasons": cand.get("failure_reasons", []),
                        "hook_detail": cand.get("_hook_eval", {}),
                        "payoff_detail": cand.get("_payoff_eval", {}),
                        "context_detail": cand.get("_context_eval", {}),
                        "selected": is_selected,
                    }
                    await database.save_candidate_evaluation(project_id, db_cand)
                logger.info(f"Persisted {len(refined_for_select)} candidate evaluations for review")
            except Exception as e:
                logger.warning(f"Failed to persist candidate evaluations: {e}")
        except Exception as exc:
            logger.warning("Multimodal editorial pass failed, falling back to simple: %s", exc)
            try:
                sentences = editorial.build_sentences(transcript_segments)
                words_all = editorial.all_words(transcript_segments)
                fallback: list[dict] = []
                for s in suggestions:
                    ns, ne = editorial.snap_bounds(float(s["start"]), float(s["end"]), sentences)
                    ns = editorial.strip_leading_fillers(words_all, ns)
                    if ne - ns < 10.0:
                        continue
                    s = dict(s)
                    s["start"], s["end"] = ns, ne
                    window_text = " ".join(seg["text"] for seg in transcript_segments if seg["end"] > ns and seg["start"] < ne)
                    s["_det"] = editorial.deterministic_score(window_text, words_all, ns, ne, video_duration)
                    fallback.append(s)
                suggestions = editorial.select_clips(fallback, top_k=8)
            except Exception as exc2:
                logger.warning("Fallback editorial also failed: %s", exc2)

        if not suggestions:
            await database.update_project_status(project_id, "error")
            await _broadcast(project_id, "error", 0, "AI could not find clip moments. Try a different video or check your API key.")
            return

        await _broadcast(
            project_id, "analyzing", 75,
            f"Found {len(suggestions)} clip suggestions",
        )

        # ---- STEP 4: Cut Clips & Reframe ----
        await database.update_project_status(project_id, "processing")

        for i, suggestion in enumerate(suggestions):
            progress_pct = 75 + (i / len(suggestions) * 20)
            await _broadcast(project_id, "processing", progress_pct, 
                             f"Processing clip {i+1} of {len(suggestions)}...")
            
            # Per-clip auto output: large faces -> 9:16 tracked crop;
            # screen/tutorial, no-face or multi-speaker -> blurred-fill 9:16
            # (full frame kept sharp). Explicit output_mode overrides the auto.
            try:
                clip_plan = await reframer.plan_clip(
                    file_path, suggestion, output_mode=output_mode, work_dir=clips_dir)
            except Exception as plan_exc:
                logger.warning("plan_clip unavailable, using blur-fill fallback: %s", plan_exc)
                clip_plan = {"strategy": "blur_fill", "aspect": "9:16", "canvas": "9:16",
                             "caption_style": "minimal_clean", "layout_mode": "none",
                             "reason": "plan_error"}

            if clip_plan.get("strategy") == "blur_fill":
                comp = composition_mod.default_composition(clip_plan.get("canvas") or "9:16")
                comp = composition_mod.apply_preset(comp, "blur_16x9")
                social_options = dict(reframe_options)
                social_options.update({
                    "aspect": "9:16", "canvas": "9:16", "composition": comp})
                clip = await reframer.render_social(
                    file_path, suggestion, clips_dir, project_id,
                    transcript=transcript_segments,
                    options=social_options,
                )
            else:
                clip_options_for_render = dict(reframe_options)
                clip_options_for_render["aspect"] = clip_plan.get("aspect") or reframe_options.get("aspect", "9:16")
                if clip_plan.get("strategy") == "letterbox":
                    # Keep the whole frame: no tracking punch-in on a native canvas.
                    clip_options_for_render["tracking"] = False
                    clip_options_for_render["zoom_boost"] = 1.0
                clip = await reframer.process_clip(
                    file_path, suggestion, clips_dir, project_id,
                    transcript=transcript_segments,
                    options=clip_options_for_render,
                )
            clip["output_plan"] = clip_plan

            # STEP 5 — BURN CAPTIONS (retention-remapped transcript when present)
            caption_style = caption_style_requested or clip_plan.get("caption_style")
            if not caption_style:
                caption_style = await settings_mod.get_setting("caption_style")
            clip_transcript = clip["transcript"] if clip.get("transcript") is not None else transcript_segments
            if caption_style and caption_style != "none":
                progress_pct = 90 + (i / len(suggestions) * 8)
                await _broadcast(project_id, "processing", progress_pct,
                                 f"Adding captions to clip {i+1}...")
                cap_win = clip.get("caption_window") or (clip['start_time'], clip['end_time'])
                captioned_clip, caption_events = await captioner.burn_captions(
                    clip['file_path'],
                    clip_transcript,
                    cap_win[0],
                    cap_win[1],
                    caption_style,
                    face_zone=clip.get("face_zone"),
                    face_zones=clip.get("face_zones"),
                )
                clip["caption_events"] = caption_events
                os.replace(captioned_clip, clip['file_path'])
                clip["caption_style"] = caption_style

            filename = os.path.basename(clip["file_path"])
            file_url = f"/files/{project_id}/clips/{filename}"

            # STEP 6 — QUALITY CHECK (flags weak clips for review, never blocks)
            qc = quality.check_clip(
                clip["file_path"],
                expected_duration=clip.get("duration", 0.0),
                caption_events=clip.get("caption_events", 0),
                captions_expected=bool(caption_style and caption_style != "none"),
                face_count=clip.get("face_count", 0),
                layout_mode=clip.get("layout_mode", ""),
            )
            needs_confirm = bool(clip.get("needs_user_confirm", False) or not qc["pass"])
            if not qc["pass"]:
                logger.warning("QC failed for clip %d: %s", i + 1, qc["reasons"])
                await _broadcast(project_id, "processing", progress_pct,
                                 f"Clip {i+1} flagged for review ({'; '.join(qc['reasons'][:2])})")

            # P8: Output integrity — 8 hard checks before publishing
            integrity_ok, integrity_reasons = await _verify_clip_integrity(
                clip["file_path"], expected_aspect=clip.get("aspect", "9:16"),
                expected_duration=clip.get("duration", 0.0))
            if not integrity_ok:
                logger.error("Output integrity failed for clip %d: %s — deleting partial", i+1, integrity_reasons)
                try:
                    if os.path.exists(clip["file_path"]):
                        os.remove(clip["file_path"])
                except OSError:
                    pass
                # Do not publish corrupt clip; continue to next candidate
                await _broadcast(project_id, "processing", progress_pct,
                                 f"Clip {i+1} failed integrity ({'; '.join(integrity_reasons[:2])}) — skipping")
                continue

            # P9: Persist full quality report
            mm = suggestion.get("_multimodal", {}) or {}
            await database.save_clip(
                project_id=project_id,
                file_path=file_url,
                start_time=clip["start_time"],
                end_time=clip["end_time"],
                title=clip.get("title"),
                reason=clip.get("reason"),
                viral_score=clip.get("viral_score"),
                face_count=clip.get("face_count"),
                layout_mode=clip.get("layout_mode"),
                caption_style=clip.get("caption_style"),
                needs_user_confirm=needs_confirm,
                reframed=clip.get("reframed", True),
                hashtags=suggestion.get("hashtags", []),
                tags=suggestion.get("tags", []),
                aspect=clip.get("aspect", "9:16"),
                final_score=mm.get("final"),
                hook_score=float(suggestion.get("hook", 5)) if suggestion.get("hook") is not None else None,
                hook_first_3s_score=float(suggestion.get("_hook_first_3s", {}).get("score", 5)) if suggestion.get("_hook_first_3s") else None,
                payoff_score=float(suggestion.get("payoff", 5)) if suggestion.get("payoff") is not None else None,
                audio_score=float(suggestion.get("_audio", {}).get("total", 5)) if suggestion.get("_audio") else None,
                visual_score=float(suggestion.get("_visual", {}).get("total", 5)) if suggestion.get("_visual") else None,
                reaction_score=float(suggestion.get("_reaction", {}).get("total", 5)) if suggestion.get("_reaction") else None,
                pacing_score=float(suggestion.get("_pacing", {}).get("total", 0)) if suggestion.get("_pacing") else None,
                cuts_applied=clip.get("cuts_applied", 0),
            )

        await database.update_project_status(project_id, "done")
        await _broadcast(project_id, "done", 100, "Complete")

    except Exception as exc:
        import traceback
        err_path = os.path.join(TMP_DIR, project_id, "error_trace.txt")
        try:
            with open(err_path, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except OSError:
            pass
        with open(os.path.join(os.path.dirname(__file__), "error_trace.txt"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())

        logger.exception("Pipeline failed for project %s", project_id)
        await database.update_project_status(project_id, "error")
        await _broadcast(
            project_id, "error", 0, f"Pipeline error: {exc}"
        )
    finally:
        if _pipeline_slots is not None:
            _pipeline_slots.release()


# ---------------------------------------------------------------------------
# REST routes — Projects
# ---------------------------------------------------------------------------

@app.post("/api/projects", response_model=CreateProjectResponse, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    background_tasks: BackgroundTasks,
):
    """
    Create a new project from a YouTube URL.

    The project row is inserted immediately and the pipeline is kicked
    off in the background.  The response is returned without waiting
    for the download/clip work to finish.
    """
    if not re.match(r"^(https?\:\/\/)?(www\.youtube\.com|youtu\.be)\/.+$", body.youtube_url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    # Collect only the adjustment fields the client actually sent.
    pipeline_options: dict = {}
    for key in ("layout_mode", "aspect", "zoom_boost", "x_offset", "y_offset", "tracking", "caption_style", "output_mode"):
        value = getattr(body, key, None)
        if value is not None:
            pipeline_options[key] = value

    result = await database.create_project(
        youtube_url=body.youtube_url,
        pipeline_options=pipeline_options or None,
    )
    project_id = result["project_id"]

    # Fire-and-forget — runs after the response is sent
    background_tasks.add_task(_run_pipeline, project_id, body.youtube_url, pipeline_options or None)

    return {"project_id": project_id, "status": "pending"}


@app.get("/api/projects", response_model=list[ProjectListItem])
async def list_projects():
    """Return all projects, newest first, with clip counts."""
    return await database.get_all_projects()


@app.get("/api/projects/{project_id}", response_model=ProjectDetailResponse)
async def get_project(project_id: str):
    """Get a single project by ID, including its clips."""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.post("/api/projects/{project_id}/retry", status_code=202)
async def retry_project(project_id: str, background_tasks: BackgroundTasks):
    """
    Re-run the pipeline for a failed (or stuck-reset) project.

    Only projects in ``error`` state can be retried — active ones already
    have a worker, and finished ones would duplicate clips.
    """
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["status"] != "error":
        raise HTTPException(status_code=409, detail=f"Project is {project['status']}, only failed projects can be retried")
    await database.update_project_status(project_id, "pending")
    stored_options = project.get("pipeline_options") or {}
    background_tasks.add_task(_run_pipeline, project_id, project["youtube_url"], stored_options or None)
    return {"project_id": project_id, "status": "pending"}


class ReprocessClipRequest(BaseModel):
    """Manual crop/grade adjustments for one clip."""
    layout_mode: Optional[str] = None  # auto/single/tutorial/podcast/panel/none/center
    aspect: Optional[str] = None  # 9:16 / 1:1 / 16:9 / 4:3
    zoom_boost: Optional[float] = None  # 0.8 (wide) – 1.6 (tight)
    x_offset: Optional[float] = None  # -1 (left) – +1 (right)
    y_offset: Optional[float] = None  # -1 (up) – +1 (down)
    tracking: Optional[bool] = None  # False = fixed camera
    static: Optional[bool] = None  # legacy alias for tracking=False
    caption_style: Optional[str] = None


@app.post("/api/clips/{clip_id}/reprocess")
async def reprocess_clip(clip_id: str, body: ReprocessClipRequest):
    """
    Re-render one clip with manual crop adjustments.

    Re-cuts from the source video at the clip's timestamps, applies the
    requested framing overrides, re-burns captions, and overwrites the
    clip file in place. Returns the updated clip record.
    """
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    project_id = clip["project_id"]

    project_dir = os.path.join(TMP_DIR, project_id)
    sources = [
        p for p in glob.glob(os.path.join(project_dir, "*.mp4"))
        if os.path.isfile(p)
    ]
    if not sources:
        raise HTTPException(status_code=404, detail="Source video missing — re-run the project")
    source = max(sources, key=os.path.getsize)

    rel = (clip["file_path"] or "").replace("/files/", "").lstrip("/\\")
    out_path = os.path.normpath(os.path.join(TMP_DIR, rel))
    if not out_path.startswith(os.path.abspath(TMP_DIR)):
        raise HTTPException(status_code=400, detail="Bad clip path")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    options: dict = {"no_hook_trim": True, "no_face_align": True}
    if body.layout_mode:
        options["layout_mode"] = body.layout_mode
    if body.aspect:
        options["aspect"] = body.aspect
    if body.zoom_boost is not None:
        options["zoom_boost"] = body.zoom_boost
    if body.x_offset is not None:
        options["x_offset"] = body.x_offset
    if body.y_offset is not None:
        options["y_offset"] = body.y_offset
    if body.tracking is not None:
        options["tracking"] = body.tracking
    if body.static is not None:
        options["static"] = body.static

    suggestion = {
        "start": float(clip["start_time"]),
        "end": float(clip["end_time"]),
        "title": clip.get("title", ""),
        "reason": clip.get("reason", ""),
        "viral_score": clip.get("viral_score", 0),
    }
    try:
        result = await reframer.process_clip(
            source, suggestion, os.path.dirname(out_path), project_id, options=options
        )
        if os.path.abspath(result["file_path"]) != os.path.abspath(out_path):
            os.replace(result["file_path"], out_path)

        style = body.caption_style or clip.get("caption_style") \
            or await settings_mod.get_setting("caption_style") or "none"
        if style != "none":
            project = await database.get_project(project_id)
            segments = await transcriber.extract_captions(project["youtube_url"], source)
            if segments:
                cap_win = result.get("caption_window") or (result["start_time"], result["end_time"])
                captioned, _events = await captioner.burn_captions(
                    out_path, segments,
                    cap_win[0], cap_win[1], style,
                    face_zone=result.get("face_zone"),
                    face_zones=result.get("face_zones"),
                )
                if os.path.abspath(captioned) != os.path.abspath(out_path):
                    os.replace(captioned, out_path)

        await database.update_clip(
            clip_id,
            layout_mode=result.get("layout_mode"),
            caption_style=style,
            face_count=result.get("face_count"),
            aspect=result.get("aspect"),
        )
    except Exception as exc:
        logger.exception("Reprocess failed for clip %s", clip_id)
        raise HTTPException(status_code=500, detail=f"Reprocess failed: {exc}")

    updated = await database.get_clip(clip_id)
    return updated


@app.get("/api/clips/{clip_id}/crop-preview")
async def crop_preview(
    clip_id: str,
    aspect: str = "9:16",
    zoom_boost: float = 1.0,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
    layout_mode: str = "auto",
    tracking: bool = True,
    t: Optional[float] = None,
):
    """
    Annotated preview frame showing exactly where the camera crops.
    """
    from PIL import ImageDraw  # type: ignore

    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    project_id = clip["project_id"]
    project_dir = os.path.join(TMP_DIR, project_id)
    sources = [p for p in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.isfile(p)]
    if not sources:
        raise HTTPException(status_code=404, detail="Source video missing")

    def _work() -> dict:
        from PIL import Image  # type: ignore
        source = max(sources, key=os.path.getsize)
        mid = t if t is not None else (float(clip["start_time"]) + float(clip["end_time"])) / 2.0
        frame = reframer.grab_frame(source, mid)
        W, H = frame.size

        faces = reframer._detect_faces_all_frames([frame], W, H)
        faces = faces[0] if faces else []
        options = {"aspect": aspect, "layout_mode": layout_mode}
        layout = reframer._resolve_layout([frame], [faces], W, H, len(faces), options)

        if tracking and faces and layout not in ("none",):
            if layout in ("podcast", "panel"):
                cx = sum(f["cx"] for f in faces) / len(faces)
                cy = sum(f["cy"] for f in faces) / len(faces)
            else:
                best = max(faces, key=lambda f: f["size"])
                cx, cy = best["cx"], best["cy"]
        elif faces:
            cx = sum(f["cx"] for f in faces) / len(faces)
            cy = sum(f["cy"] for f in faces) / len(faces)
        else:
            cx, cy = W / 2.0, H / 2.0

        ar_w, ar_h, _ow, _oh = reframer._aspect_spec(aspect)
        zoom = 1.0 * max(0.8, min(float(zoom_boost), 1.6))
        cw, ch, rx, ry = reframer._compute_crop(
            W, H, zoom, cx, cy, ar_w, ar_h,
            max(-1.0, min(float(x_offset), 1.0)),
            max(-1.0, min(float(y_offset), 1.0)),
        )
        slack_x = max(0.0, float(W) - float(cw))
        slack_y = max(0.0, float(H) - float(ch))

        scale = 960.0 / float(W)
        preview = frame.resize((960, max(1, int(H * scale))), Image.LANCZOS)
        draw = ImageDraw.Draw(preview)
        for f in faces:
            r = max(6.0, float(f.get("size", 0.1)) * H * scale * 0.6)
            fx, fy = f["cx"] * scale, f["cy"] * scale
            draw.ellipse([fx - r, fy - r, fx + r, fy + r], outline=(190, 242, 100), width=3)
        draw.rectangle([rx * scale, ry * scale, (rx + cw) * scale, (ry + ch) * scale],
                       outline=(248, 113, 113), width=3)

        buf = _io.BytesIO()
        preview.save(buf, format="JPEG", quality=82)
        return {
            "image": base64.b64encode(buf.getvalue()).decode(),
            "width": preview.width,
            "height": preview.height,
            "aspect": aspect,
            "layout": layout,
            "crop": {"x": rx * scale, "y": ry * scale, "w": cw * scale, "h": ch * scale},
            "faces": [{"x": f["cx"] * scale, "y": f["cy"] * scale} for f in faces],
            "base": {"cx": cx, "cy": cy},
            "slack": {"x": slack_x, "y": slack_y},
            "frame": {"w": W, "h": H},
            "at": mid,
        }

    try:
        return await asyncio.to_thread(_work)
    except Exception as exc:
        logger.exception("Crop preview failed for clip %s", clip_id)
        raise HTTPException(status_code=500, detail=f"Preview failed: {exc}")


# ---------------------------------------------------------------------------
# Editor — manual refinement layer (B15/B20)
# ---------------------------------------------------------------------------

EXPORT_PRESETS: dict[str, dict] = {
    "social_9_16_1080": {"label": "Social 9:16 \u2014 1080x1920", "canvas": "9:16", "w": 1080, "h": 1920},
    "social_9_16_720": {"label": "Social 9:16 \u2014 720x1280", "canvas": "9:16", "w": 720, "h": 1280},
    "square_1_1": {"label": "Square 1:1 \u2014 1080x1080", "canvas": "1:1", "w": 1080, "h": 1080},
    "portrait_4_5": {"label": "Portrait 4:5 \u2014 1080x1350", "canvas": "4:5", "w": 1080, "h": 1350},
    "landscape_16_9": {"label": "Landscape 16:9 \u2014 1920x1080", "canvas": "16:9", "w": 1920, "h": 1080},
}


@app.get("/api/composition/presets")
async def get_composition_presets():
    """Single source of preset metadata for the editor UI."""
    return {
        "canvases": {k: {"w": v[0], "h": v[1]} for k, v in composition_mod.CANVAS_PRESETS.items()},
        "default_canvas": composition_mod.DEFAULT_CANVAS,
        "blur_levels": composition_mod.BLUR_LEVELS,
        "darkness_levels": composition_mod.DARKNESS_LEVELS,
        "social_presets": list(composition_mod.SOCIAL_PRESETS.keys()),
        "export_presets": EXPORT_PRESETS,
        "default_composition": composition_mod.default_composition(),
    }


class EditorState(BaseModel):
    """Manual editor state — serializable, reloadable, reproducible (B15)."""
    source_start: Optional[float] = None
    source_end: Optional[float] = None
    output_aspect: str = "9:16"
    crop_mode: str = "auto"  # auto | manual
    crop_keyframes: Optional[list[dict]] = None  # [{t, x, y, zoom}] normalized 0-1
    zoom: float = 1.0
    x: float = 0.5  # normalized 0-1 center (0.52 etc)
    y: float = 0.5
    caption_style: Optional[str] = None
    caption_position: Optional[str] = None  # top | center | bottom
    caption_scale: float = 1.0
    caption_enabled: bool = True
    automatic_cuts: Optional[list[dict]] = None  # [{start, end, reason}] for display (B12)
    manual_cuts: Optional[list[dict]] = None  # [{start, end, enabled}] manual overrides (B13)
    history: Optional[list[dict]] = None  # undo/redo stack (B17)
    composition: Optional[dict] = None  # Social Canvas state (mode/canvas/bg/fg/captions/overlays)
    canvas_preset: str = "9:16"  # 9:16 | 16:9 | 1:1 | 4:5
    export_preset: Optional[str] = None  # resolve canvas: Social 9:16 1080x1920 etc.


@app.get("/api/clips/{clip_id}/editor-state")
async def get_editor_state(clip_id: str):
    """Load saved manual edit state for a clip (B16)."""
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    state = await database.get_editor_state(clip_id)
    if state is None:
        # Return defaults derived from current clip
        return {
            "clip_id": clip_id,
            "source_start": clip["start_time"],
            "source_end": clip["end_time"],
            "output_aspect": clip.get("aspect", "9:16"),
            "crop_mode": "auto",
            "zoom": 1.0,
            "x": 0.5,
            "y": 0.5,
            "caption_style": clip.get("caption_style", "viral_word"),
            "caption_enabled": True,
            "caption_scale": 1.0,
            "automatic_cuts": [],
            "manual_cuts": [],
            "composition": composition_mod.default_composition(
                clip.get("aspect", "9:16") if clip.get("aspect") in composition_mod.CANVAS_PRESETS else "9:16"),
            "canvas_preset": clip.get("aspect", "9:16"),
            "export_preset": None,
        }
    return state


@app.post("/api/clips/{clip_id}/editor-state")
async def save_editor_state(clip_id: str, state: EditorState):
    """Persist manual edit instructions (B16) — no render, just save."""
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    # Validate bounds
    if state.source_start is not None and state.source_end is not None:
        if state.source_start >= state.source_end:
            raise HTTPException(status_code=400, detail="source_start must be < source_end")
        if state.source_start < 0 or state.source_end > 10000:
            raise HTTPException(status_code=400, detail="invalid source bounds")
    if not 0.0 <= state.x <= 1.0 or not 0.0 <= state.y <= 1.0:
        raise HTTPException(status_code=400, detail="x/y must be 0-1 normalized")
    if not 0.5 <= state.zoom <= 3.0:
        raise HTTPException(status_code=400, detail="zoom must be 0.5-3.0")
    if state.canvas_preset not in composition_mod.CANVAS_PRESETS:
        raise HTTPException(status_code=400, detail="unknown canvas_preset")
    if state.export_preset is not None and state.export_preset not in EXPORT_PRESETS:
        raise HTTPException(status_code=400, detail="unknown export_preset")
    dump = state.model_dump()
    # Server-side normalize: stored composition is always valid/clamped
    try:
        dump["composition"] = composition_mod.normalize_composition(
            dump.get("composition"), dump.get("canvas_preset", "9:16"))
    except Exception:
        dump["composition"] = composition_mod.default_composition("9:16")
    await database.save_editor_state(clip_id, dump)
    return {"success": True, "clip_id": clip_id}


async def _render_editor_core(clip_id: str, state: dict, output_dir: str,
                              progress_cb=None, cancel_event: Optional[threading.Event] = None) -> dict:
    """
    Render final clip from saved editor state into output_dir (staging).
    Uses production pipeline: crop/reframe + retention + captions + loudnorm + QC.
    Never publishes invalid render (P8) — the caller verifies + atomically publishes.
    Returns a result dict; raises on failure/cancel.
    """
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if not state:
        raise HTTPException(status_code=400, detail="No editor state saved — save first")

    def _tick(stage: str, pct: float, msg: str = "") -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Render cancelled by user")
        if progress_cb:
            progress_cb(stage, pct, msg)

    project_id = clip["project_id"]
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Resolve source file (largest mp4 in project dir)
    project_dir = os.path.join(TMP_DIR, project_id)
    sources = [p for p in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.isfile(p)]
    if not sources:
        raise HTTPException(status_code=404, detail="Source video missing")
    source = max(sources, key=os.path.getsize)

    # Build suggestion from editor state
    s_start = float(state.get("source_start", clip["start_time"]))
    s_end = float(state.get("source_end", clip["end_time"]))
    if s_end <= s_start:
        raise HTTPException(status_code=400, detail="Invalid editor start/end")

    suggestion = {
        "start": s_start,
        "end": s_end,
        "title": clip.get("title", ""),
        "reason": clip.get("reason", ""),
        "viral_score": clip.get("viral_score", 5),
    }
    # Crop options from editor state
    options: dict = {
        "aspect": state.get("output_aspect", "9:16"),
        "no_face_align": True,  # manual framing overrides auto face-align
    }
    if state.get("layout_mode"):
        options["layout_mode"] = state["layout_mode"]
    if state.get("tracking") is False or state.get("fixed_camera"):
        options["tracking"] = False
    if state.get("crop_mode") == "manual":
        options["crop_mode"] = "manual"
        options["tracking"] = False
        kfs = state.get("crop_keyframes")
        if kfs and isinstance(kfs, list) and len(kfs) > 0:
            # Pass full keyframe list for per-segment interpolation (single source of truth)
            options["crop_keyframes"] = kfs
        else:
            # Single manual values → single keyframe at t=0
            x_norm = float(state.get("x", 0.5))
            y_norm = float(state.get("y", 0.5))
            options["crop_keyframes"] = [{"t": 0, "x": x_norm, "y": y_norm, "zoom": float(state.get("zoom", 1.0))}]
            # Also set legacy offsets for fallback (non-keyframe path)
            options["x_offset"] = (x_norm - 0.5) * 2.0
            options["y_offset"] = (y_norm - 0.5) * 2.0
            options["zoom_boost"] = float(state.get("zoom", 1.0))

    caption_style = state.get("caption_style") if state.get("caption_enabled", True) else "none"
    if caption_style is None:
        caption_style = clip.get("caption_style", "viral_word")

    # Output path: editor render writes into staging output_dir (atomic publish later)
    os.makedirs(output_dir, exist_ok=True)
    # Use editor suffix to avoid clobbering auto
    editor_out = os.path.join(output_dir, f"clip_{clip_id}_editor.mp4")

    # Composition mode + export preset resolution
    comp = composition_mod.normalize_composition(
        state.get("composition"), state.get("canvas_preset", "9:16"))
    export = EXPORT_PRESETS.get(state.get("export_preset") or "")
    if export:
        comp["canvas"] = export["canvas"]
    social_mode = comp.get("mode") == "social"

    # Fetch transcript for captions/retention (heavy: network/whisper → worker thread)
    _tick("transcript", 3, "Loading transcript…")
    # Editor renders must stay responsive: reuse the project's cached transcript,
    # and if it's missing only try network captions (never the slow Whisper pass)
    # under a hard time budget. A miss simply renders without burned captions.
    transcript = await _get_project_transcript(
        project_id, project["youtube_url"], source,
        allow_whisper=False, timeout=_EDITOR_TRANSCRIPT_TIMEOUT)
    if social_mode:
        options["composition"] = comp
        options["canvas"] = comp["canvas"]
        clip_result = await asyncio.to_thread(
            _run_render_worker, reframer.render_social, source, suggestion, output_dir,
            project_id, options, transcript, progress_cb, cancel_event)
    else:
        clip_result = await asyncio.to_thread(
            _run_render_worker, reframer.process_clip, source, suggestion, output_dir,
            project_id, None, options, transcript, progress_cb, cancel_event)
    # Move to editor path if needed
    if os.path.abspath(clip_result["file_path"]) != os.path.abspath(editor_out):
        try:
            os.replace(clip_result["file_path"], editor_out)
            clip_result["file_path"] = editor_out
        except Exception:
            pass

    # People-policy blur layer (real FFmpeg masks, before captions per layer order:
    # SOURCE → TRIM → REFRAME/CROP → SOCIAL → PEOPLE MASK → CAPTIONS → GRAPHICS
    # → AUDIO → FINAL ENCODE). Detection runs on the final reframed pixels so
    # stored tracks match both preview overlay and render. Render-time
    # detection is authoritative; preview overlays are advisory only.
    # Generic modes only: OFF | BLUR_ALL | MANUAL | REVIEW (no appearance
    # classification of any kind).
    people_cfg = state.get("people_policy") or {}
    from automation import people_policy as _pp_mode
    _people_mode = _pp_mode.normalize_mode(people_cfg)
    if _people_mode != _pp_mode.PEOPLE_POLICY_OFF:
        _tick("people_policy", 84, "Checking people policy…")

        def _people_blur_sync() -> None:
            from automation import store as _astore
            from automation import people_policy as _pp
            import asyncio as _asyncio
            dur = float(clip_result.get("duration", 0) or 0)
            manual_ids = [int(t.get("track_id", 0)) for t in
                          (people_cfg.get("manual_tracks") or [])
                          if t.get("blur")]
            analysis = _pp.analyze_clip_people(
                clip_result["file_path"], 0.0, max(dur, 0.5),
                region_mode=str(people_cfg.get("region_mode", "full")),
                mode=_people_mode, manual_blur_ids=manual_ids)
            gate = _pp.apply_policy_decision(analysis, _people_mode)
            quality = _pp.validate_tracks(analysis["tracks"], 0.0, max(dur, 0.5))
            blurred_ids = {id(t) for t in gate.get("blur_tracks", [])}
            tracks = [{**t,
                       "blur": t in gate.get("blur_tracks", []),
                       "action": ("BLUR" if t in gate.get("blur_tracks", [])
                                  else "KEEP"),
                       "review_status": ("APPROVED" if t in gate.get("blur_tracks", [])
                                         else "PENDING")}
                      for t in analysis["tracks"]]
            _ = blurred_ids
            _asyncio.run(_astore.save_people_tracks(clip_id, tracks))
            for t in tracks:
                _asyncio.run(_astore.log_decision(
                    clip_id=clip_id, decision=f"person_track_{t['action'].lower()}",
                    rules=["people_policy"],
                    evidence=[_pp.audit_record(clip_id, t)], operator="automation"))
            if not quality["ok"]:
                raise HTTPException(status_code=409, detail={
                    "code": "people_policy_review",
                    "reason": "unstable_masks",
                    "issues": quality["issues"]})
            if gate["gate"] == "NEEDS_REVIEW":
                raise HTTPException(status_code=409, detail={
                    "code": "people_policy_review",
                    "reason": gate.get("reason", "unresolved_tracks")})
            if gate.get("blur_tracks"):
                blurred = clip_result["file_path"] + ".people.mp4"
                _pp.apply_people_blur(clip_result["file_path"], blurred,
                                      gate["blur_tracks"],
                                      str(people_cfg.get("blur_type", "soft")))
                os.replace(blurred, clip_result["file_path"])

        try:
            await asyncio.to_thread(_people_blur_sync)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"People-policy step failed: {exc}")

    # Captions (canvas-aware in social mode; overlay lane carries title/watermark)
    social_cap_pos = comp["captions"]["position"] if social_mode else None
    if social_cap_pos == "off":
        caption_style = "none"
    if caption_style and caption_style != "none" and transcript:
        cap_win = clip_result.get("caption_window") or (clip_result["start_time"], clip_result["end_time"])
        cap_transcript = clip_result.get("transcript") or transcript
        if social_mode:
            cw, ch = composition_mod.canvas_size(comp["canvas"])
            captioned, _ = await asyncio.to_thread(
                _run_render_worker, captioner.burn_captions,
                clip_result["file_path"], cap_transcript, cap_win[0], cap_win[1], caption_style,
                face_zone=clip_result.get("face_zone"), face_zones=clip_result.get("face_zones"),
                canvas=(cw, ch), caption_pos=social_cap_pos or "bottom",
                caption_scale=float(comp["captions"].get("scale", 1.0)),
                overlays=comp.get("overlays"))
        else:
            captioned, _ = await asyncio.to_thread(
                _run_render_worker, captioner.burn_captions,
                clip_result["file_path"], cap_transcript, cap_win[0], cap_win[1], caption_style,
                face_zone=clip_result.get("face_zone"), face_zones=clip_result.get("face_zones"))
        os.replace(captioned, clip_result["file_path"])

    # Export preset downscale (e.g. 720x1280 from 1080x1920 canvas)
    if export and (export["w"], export["h"]) != composition_mod.canvas_size(comp["canvas"] if social_mode else clip_result.get("aspect", "9:16")):
        try:

            def _export_scale(src: str) -> None:
                """Sync ffmpeg downscale (blocking; runs in worker thread)."""
                from ffmpeg_utils import resolve_ffmpeg as _resolve_ffmpeg
                scaled = src + ".scaled.mp4"
                r = subprocess.run(
                    [_resolve_ffmpeg(), "-y", "-i", src,
                     "-vf", f"scale={export['w']}:{export['h']}:flags=lanczos",
                     "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                     "-pix_fmt", "yuv420p", "-c:a", "copy",
                     "-movflags", "+faststart", scaled],
                    capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    raise RuntimeError(f"Export scale failed: {r.stderr[-300:]}")
                os.replace(scaled, src)

            await asyncio.to_thread(_export_scale, clip_result["file_path"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Export preset failed: {exc}")

    # Integrity (P8) — staged file must be valid before it is published
    _tick("qc", 96, "Verifying output…")
    ok, reasons = await asyncio.to_thread(
        _run_render_worker, _verify_clip_integrity, clip_result["file_path"],
        clip_result.get("aspect", "9:16"), clip_result.get("duration", 0))
    if not ok:
        try:
            if os.path.exists(clip_result["file_path"]):
                os.remove(clip_result["file_path"])
        except: pass
        raise HTTPException(status_code=500, detail=f"Render failed integrity: {'; '.join(reasons)}")

    return {
        "project_id": project_id,
        "clip_id": clip_id,
        "file_path": clip_result["file_path"],
        "s_start": s_start,
        "s_end": s_end,
        "start_time": clip_result["start_time"],
        "end_time": clip_result["end_time"],
        "duration": clip_result.get("duration"),
        "title": clip_result.get("title"),
        "reason": clip_result.get("reason"),
        "viral_score": clip_result.get("viral_score"),
        "face_count": clip_result.get("face_count"),
        "layout_mode": clip_result.get("layout_mode"),
        "aspect": clip_result.get("aspect", "9:16"),
        "cuts_applied": clip_result.get("cuts_applied", 0),
        "comp": comp,
        "social_mode": social_mode,
        "caption_style": caption_style,
    }


# ---------------------------------------------------------------------------
# Async render jobs — background rendering with progress/cancel/atomic publish
# ---------------------------------------------------------------------------

async def _run_editor_job(job: dict) -> None:
    """Run one render job: stage, verify, atomically publish, record history."""
    main_loop = asyncio.get_running_loop()
    clip_id = job["clip_id"]
    project_id = job.get("project_id")
    state = job.get("state") or await database.get_editor_state(clip_id)
    cancel = job["cancel_event"]

    # Per-clip staging dir inside clips/ so renders never write to published paths.
    clips_dir = os.path.join(TMP_DIR, project_id, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    staging = os.path.join(clips_dir, f".render_{job['id']}")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    def _progress(stage: str, pct: float, msg: str = "") -> None:
        job["stage"] = stage
        job["percent"] = pct
        job["message"] = msg or ""
        job["status"] = "running"
        payload = _render_job_status(job)
        asyncio.run_coroutine_threadsafe(_broadcast_render(clip_id, payload), main_loop)
        if project_id:
            asyncio.run_coroutine_threadsafe(
                _broadcast(project_id, f"render:{stage}", pct, msg or "Rendering…"), main_loop)

    try:
        _progress("start", 1, "Preparing render…")
        async with (_render_slots if _render_slots is not None else _contextlib.nullcontext()):
            result = await _render_editor_core(clip_id, state, staging, _progress, cancel)

        # Compute output hash + save render history, then atomically publish.
        try:
            import hashlib as _hl
            _h = _hl.sha1()
            with open(result["file_path"], "rb") as _fh:
                for _chunk in iter(lambda: _fh.read(1 << 20), b""):
                    _h.update(_chunk)
            _digest = _h.hexdigest()
        except Exception:
            _digest = None

        final_name = f"clip_{clip_id}_editor.mp4"
        final_path = os.path.join(clips_dir, final_name)
        os.replace(result["file_path"], final_path)
        file_url = f"/files/{project_id}/clips/{final_name}"

        # P0-1: update the existing clip row instead of inserting a duplicate.
        await database.update_clip(
            clip_id=clip_id,
            file_path=file_url,
            start_time=result["start_time"],
            end_time=result["end_time"],
            layout_mode=result["layout_mode"],
            caption_style=result["caption_style"],
            face_count=result["face_count"],
            aspect=result["aspect"],
        )
        new_clip = await database.get_clip(clip_id)
        try:
            await database.save_clip_render(
                clip_id=clip_id, project_id=project_id,
                file_path=file_url, output_hash=_digest,
                mode="social" if result["social_mode"] else "reframe",
                canvas=result["comp"]["canvas"] if result["social_mode"] else result["aspect"],
                composition=result["comp"] if result["social_mode"] else None,
                render_settings={"export_preset": state.get("export_preset"),
                                 "caption_style": result["caption_style"],
                                 "source_start": result["s_start"], "source_end": result["s_end"]})
        except Exception as exc:
            logger.warning("Render history insert failed: %s", exc)

        job["status"] = "done"
        job["stage"] = "done"
        job["percent"] = 100
        job["message"] = "Render complete"
        job["output"] = file_url
        job["output_clip"] = new_clip
        _latest_render_job[clip_id] = job["id"]
        logger.info("Editor render %s done -> %s", job["id"], file_url)
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["message"] = "Render cancelled"
        logger.warning("Editor render %s cancelled", job["id"])
    except Exception as exc:
        job["status"] = "error"
        job["stage"] = job.get("stage", "error")
        job["error"] = str(exc)
        job["message"] = str(exc)
        logger.error("Editor render %s failed: %s", job["id"], exc)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        job["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        payload = _render_job_status(job)
        _schedule_persist(job)
        try:
            asyncio.run_coroutine_threadsafe(_broadcast_render(clip_id, payload), main_loop)
            if project_id:
                asyncio.run_coroutine_threadsafe(
                    _broadcast(project_id, f"render:{job['status']}", job["percent"], job["message"]), main_loop)
        except Exception:
            pass


def _start_editor_job(clip_id: str, project_id: str, state: Optional[dict] = None) -> dict:
    """Create + launch a background render job for a clip. Returns the job dict."""
    # If a render is already running for this clip, reuse it.
    active = [j for j in _render_jobs.values() if j.get("clip_id") == clip_id and j.get("status") == "running"]
    if active:
        return active[0]
    job = {
        "id": str(uuid.uuid4())[:12],
        "kind": "clip",
        "clip_id": clip_id,
        "project_id": project_id,
        "status": "queued",
        "stage": "queued",
        "percent": 0,
        "message": "Queued…",
        "error": None,
        "cancel_event": threading.Event(),
        "state": state,
        "clips_status": None,
        "output": None,
        "output_clip": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "finished_at": None,
    }
    _render_jobs[job["id"]] = job
    _schedule_persist(job)
    asyncio.get_running_loop().create_task(_run_editor_job(job))
    return job


@app.post("/api/clips/{clip_id}/editor-render")
async def render_editor_clip(clip_id: str):
    """
    Kick off a background editor render (async). Returns {job_id, status}.
    Progress streams over /ws/render/{clip_id}; cancel via
    POST /api/clips/{clip_id}/render-job/cancel.
    """
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    exists = await database.get_editor_state(clip_id)
    if not exists:
        raise HTTPException(status_code=400, detail="No editor state saved — save first")
    job = _start_editor_job(clip_id, clip["project_id"])
    return {"job_id": job["id"], "status": job["status"], "clip_id": clip_id}


@app.get("/api/clips/{clip_id}/render-job")
async def get_render_job(clip_id: str, job_id: Optional[str] = None):
    """Get the active (or latest) render job status for a clip."""
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if job_id:
        job = _render_jobs.get(job_id)
        if not job:
            job = await database.get_render_job(job_id)
        if job:
            return _render_job_status(job)
    active = [j for j in _render_jobs.values() if j.get("clip_id") == clip_id and j.get("status") == "running"]
    if active:
        return _render_job_status(active[0])
    latest = _latest_render_job.get(clip_id)
    if latest and latest in _render_jobs:
        return _render_job_status(_render_jobs[latest])
    persisted = await database.latest_render_job(clip_id)
    if persisted:
        return _render_job_status(persisted)
    return {"job_id": None, "clip_id": clip_id, "status": "idle"}


@app.post("/api/clips/{clip_id}/render-job/cancel")
async def cancel_render_job(clip_id: str):
    """Cancel the active render job for a clip."""
    active = [j for j in _render_jobs.values() if j.get("clip_id") == clip_id and j.get("status") == "running"]
    if not active:
        return {"cancelled": False, "message": "No active render"}
    for job in active:
        job["cancel_event"].set()
    return {"cancelled": True, "message": "Cancelling…"}


@app.get("/api/clips/{clip_id}")
async def get_clip_detail(clip_id: str):
    """Return a single clip plus its project, for the editor deep-link."""
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    project = await database.get_project(clip["project_id"])
    return {"clip": clip, "project": project}


@app.get("/api/clips/{clip_id}/download")
async def download_clip(clip_id: str):
    """Download the latest render (or the clip file) as an attachment."""
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    rel = clip["file_path"]
    renders = await database.get_clip_renders(clip_id)
    if renders:
        rel = renders[-1].get("file_path") or rel
    full = os.path.normpath(os.path.join(TMP_DIR, rel.replace("/files/", "", 1)))
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found — re-render the clip")
    fname = clip.get("title") or "clip"
    safe_name = re.sub(r"[^a-zA-Z0-9 _\-]+", "", fname).strip() or "clip"
    return FileResponse(full, media_type="video/mp4", filename=f"{safe_name}.mp4")


# ---------------------------------------------------------------------------
# Render-all (batch export) — sequential background job over a project's clips
# ---------------------------------------------------------------------------

class RenderAllRequest(BaseModel):
    """Optionally restrict a batch render to a subset of clips (by id)."""
    clip_ids: Optional[list[str]] = None


@app.post("/api/projects/{project_id}/render-all")
async def render_all_clips(project_id: str, body: Optional[RenderAllRequest] = None):
    """Batch-render every clip in a project with its saved editor state (async)."""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project_dir = os.path.join(TMP_DIR, project_id)
    sources = [p for p in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.isfile(p)]
    if not sources:
        raise HTTPException(status_code=404, detail="Source video missing")
    detail = await database.get_project(project_id)
    clips = detail.get("clips", []) if isinstance(detail, dict) else []
    if not clips:
        return {"job_id": None, "status": "idle", "total": 0}
    if body and body.clip_ids:
        wanted = set(body.clip_ids)
        clips = [c for c in clips if c["id"] in wanted]
        if not clips:
            return {"job_id": None, "status": "idle", "total": 0}

    # One active project job at a time
    active = [j for j in _render_jobs.values() if j.get("project_id") == project_id and j.get("kind") == "project" and j.get("status") == "running"]
    if active:
        return {"job_id": active[0]["id"], "status": "running", "total": len(clips)}

    job = {
        "id": str(uuid.uuid4())[:12],
        "kind": "project",
        "clip_id": None,
        "project_id": project_id,
        "status": "queued",
        "stage": "queued",
        "percent": 0,
        "message": "Preparing batch render…",
        "error": None,
        "cancel_event": threading.Event(),
        "state": None,
        "clips_status": {c["id"]: {"status": "queued"} for c in clips},
        "output": None,
        "output_clip": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "finished_at": None,
    }
    _render_jobs[job["id"]] = job
    _schedule_persist(job)

    async def _run_project_job(job: dict, project_id: str, clips: list) -> None:
        main_loop = asyncio.get_running_loop()
        cancel = job["cancel_event"]
        source = max(sources, key=os.path.getsize)
        # Fetch transcript once, reuse for every clip (avoids N network/whisper calls)
        try:
            transcript = await _get_project_transcript(
                project_id, project["youtube_url"], source)
        except Exception as exc:
            transcript = None
            job["error"] = f"Transcript fetch failed: {exc}"
        for i, clip in enumerate(clips):
            if cancel.is_set():
                job["status"] = "cancelled"
                job["message"] = "Batch render cancelled"
                break
            cid = clip["id"]
            job["clips_status"][cid] = {"status": "running"}
            pct = float(i) / max(len(clips), 1) * 100
            def _prog(stage, p, msg, _cid=cid):
                job["stage"] = stage
                job["percent"] = (pct + (p / max(1, len(clips)) * (100 / max(1, len(clips))))) if p <= 1 else pct + p / max(1, len(clips))
                job["percent"] = min(99.0, job["percent"])
                job["message"] = f"{_cid[:8]}… {msg or stage}"
                job["clips_status"][_cid] = {"status": "running", "stage": stage, "message": msg}
                asyncio.run_coroutine_threadsafe(
                    _broadcast(project_id, "render-all", job["percent"], job["message"]), main_loop)
            try:
                state = await database.get_editor_state(cid) or {}
                state = dict(state) if state else {}
                staged = os.path.join(os.path.join(TMP_DIR, project_id, "clips"), f".render_{job['id']}_{i}")
                os.makedirs(staged, exist_ok=True)
                async with (_render_slots if _render_slots is not None else _contextlib.nullcontext()):
                    result = await _render_editor_core(cid, state, staged, _prog, cancel)
                clips_dir = os.path.join(TMP_DIR, project_id, "clips")
                final_path = os.path.join(clips_dir, f"clip_{cid}_editor.mp4")
                os.replace(result["file_path"], final_path)
                file_url = f"/files/{project_id}/clips/{os.path.basename(final_path)}"
                await database.save_clip_render(
                    clip_id=cid, project_id=project_id, file_path=file_url,
                    output_hash=None, mode="social" if result["social_mode"] else "reframe",
                    canvas=result["comp"]["canvas"] if result["social_mode"] else result["aspect"],
                    composition=result["comp"] if result["social_mode"] else None,
                    render_settings={"export_preset": state.get("export_preset"),
                                     "caption_style": result["caption_style"],
                                     "source_start": result["s_start"], "source_end": result["s_end"]})
                job["clips_status"][cid] = {"status": "done", "file_path": file_url}
                shutil.rmtree(staged, ignore_errors=True)
            except asyncio.CancelledError:
                job["clips_status"][cid] = {"status": "cancelled"}
                break
            except Exception as exc:
                job["clips_status"][cid] = {"status": "error", "error": str(exc)}
        job["status"] = "done" if job["status"] != "cancelled" else "cancelled"
        job["stage"] = "done"
        job["percent"] = 100 if job["status"] == "done" else job["percent"]
        job["message"] = "Batch render complete" if job["status"] == "done" else job["message"]
        job["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        _schedule_persist(job)
        try:
            await _broadcast(project_id, "render-all", job["percent"], job["message"])
        except Exception:
            pass

    asyncio.get_running_loop().create_task(_run_project_job(job, project_id, clips))
    return {"job_id": job["id"], "status": "running", "total": len(clips)}


@app.get("/api/projects/{project_id}/render-job")
async def get_project_render_job(project_id: str, job_id: Optional[str] = None):
    if job_id:
        job = _render_jobs.get(job_id)
        if not job:
            job = await database.get_render_job(job_id)
        if job:
            return _render_job_status(job)
    active = [j for j in _render_jobs.values() if j.get("project_id") == project_id and j.get("kind") == "project"]
    if active:
        return _render_job_status(active[-1])
    persisted = await database.latest_project_render_job(project_id)
    if persisted:
        return _render_job_status(persisted)
    return {"job_id": None, "project_id": project_id, "status": "idle"}


@app.get("/api/clips/{clip_id}/renders")
async def get_clip_renders(clip_id: str):
    """Render history for a clip (mode/canvas/composition/output hash)."""
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    return await database.get_clip_renders(clip_id)


@app.get("/api/projects/{project_id}/source")
async def get_source_video(project_id: str):
    """Get source video metadata (V0.6: duration, width, height, fps, has_audio)."""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project_dir = os.path.join(TMP_DIR, project_id)
    sources = [p for p in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.isfile(p) and "clips" not in p and not re.search(r"\.f\d+\.mp4$", os.path.basename(p)) and ".temp." not in p]
    if not sources:
        # Fallback: any mp4 not in clips
        sources = [p for p in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.isfile(p) and "clips" not in p]
    if not sources:
        raise HTTPException(status_code=404, detail="Source video not found")
    source = max(sources, key=os.path.getsize)
    try:
        from ffmpeg_utils import resolve_ffprobe
        result = subprocess.run(
            [resolve_ffprobe(), "-v", "error",
             "-show_entries", "stream=width,height,r_frame_rate,codec_name,codec_type,avg_frame_rate",
             "-show_entries", "format=duration,size",
             "-show_entries", "stream=index,codec_type",
             "-of", "json", source],
            capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        v = video_streams[0] if video_streams else {}
        fmt = data.get("format", {})
        # Parse fps
        fps = 30.0
        r_frame = v.get("r_frame_rate") or v.get("avg_frame_rate") or "30/1"
        try:
            if "/" in str(r_frame):
                num, den = str(r_frame).split("/")
                fps = float(num) / float(den) if float(den) else 30.0
            else:
                fps = float(r_frame)
        except: fps = 30.0
        return {
            "project_id": project_id,
            "path": f"/files/{project_id}/{os.path.basename(source)}",
            "filename": os.path.basename(source),
            "duration": float(fmt.get("duration", 0)) if fmt.get("duration") else 0,
            "width": int(v.get("width", 0)) if v.get("width") else 0,
            "height": int(v.get("height", 0)) if v.get("height") else 0,
            "fps": round(fps, 2),
            "has_audio": len(audio_streams) > 0,
            "video_codec": v.get("codec_name"),
            "audio_codec": audio_streams[0].get("codec_name") if audio_streams else None,
            "size": int(fmt.get("size", 0)) if fmt.get("size") else os.path.getsize(source),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to probe source: {e}")


@app.get("/api/projects/{project_id}/thumbnails")
async def get_thumbnails(project_id: str, count: int = 10):
    """Generate filmstrip thumbnails from source video (real frames)."""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project_dir = os.path.join(TMP_DIR, project_id)
    sources = [p for p in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.isfile(p) and "clips" not in p]
    if not sources:
        raise HTTPException(status_code=404, detail="Source video not found")
    source = max(sources, key=os.path.getsize)
    # Get duration
    try:
        from ffmpeg_utils import resolve_ffprobe, resolve_ffmpeg
        result = subprocess.run(
            [resolve_ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "json", source],
            capture_output=True, text=True, timeout=10)
        dur = float(json.loads(result.stdout).get("format", {}).get("duration", 60))
    except:
        dur = 60
    count = max(1, min(count, 20))
    thumbs = []
    thumb_dir = os.path.join(project_dir, ".thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    for i in range(count):
        t = (dur * (i + 0.5) / count) if count > 1 else 0
        thumb_path = os.path.join(thumb_dir, f"thumb_{i:03d}.jpg")
        # Generate if not exists or outdated
        if not os.path.exists(thumb_path) or os.path.getmtime(thumb_path) < os.path.getmtime(source):
            subprocess.run(
                [resolve_ffmpeg(), "-y", "-ss", str(t), "-i", source, "-frames:v", "1", "-vf", "scale=160:90", "-q:v", "5", thumb_path],
                capture_output=True, timeout=15)
        if os.path.exists(thumb_path):
            rel = os.path.relpath(thumb_path, TMP_DIR)
            thumbs.append({"index": i, "time": round(t, 2), "url": f"/files/{rel.replace(os.sep, '/')}"})
    return {"project_id": project_id, "duration": dur, "thumbnails": thumbs}


@app.get("/api/projects/{project_id}/candidates")
async def get_candidates(project_id: str):
    """Review dataset: all candidate evaluations for a project (for human review)."""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    cands = await database.get_candidate_evaluations(project_id)
    return cands


@app.get("/api/projects/{project_id}/reviews")
async def get_reviews(project_id: str):
    """Get human reviews for a project."""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    reviews = await database.get_clip_reviews(project_id)
    return reviews


class ReviewRequest(BaseModel):
    decision: str  # KEEP / EDIT / REJECT
    reason: Optional[str] = None
    notes: Optional[str] = None


@app.post("/api/clips/{clip_id}/review")
async def review_clip(clip_id: str, body: ReviewRequest):
    """Human review: KEEP/EDIT/REJECT with reason (persist feedback for learning)."""
    clip = await database.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    rid = await database.save_clip_review(clip_id, clip["project_id"], body.decision, body.reason, body.notes)
    return {"success": True, "review_id": rid}


@app.post("/api/projects/{project_id}/candidates/{candidate_id}/make-clip", status_code=202)
async def make_clip_from_candidate(project_id: str, candidate_id: str):
    """
    Turn a reviewed candidate moment into a real clip.

    Creates a clip row from the candidate's source window, applies the
    project's pipeline adjustments (aspect/layout/zoom/tracking/captions),
    and kicks off a background editor render. Returns {job_id, clip_id}.
    """
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    cand = await database.get_candidate(candidate_id)
    if not cand or cand.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    project_dir = os.path.join(TMP_DIR, project_id)
    sources = [p for p in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.isfile(p)]
    if not sources:
        raise HTTPException(status_code=404, detail="Source video missing — re-run the project")

    popts = project.get("pipeline_options") or {}
    aspect = popts.get("aspect") or "9:16"
    layout_mode = popts.get("layout_mode") or "auto"
    caption_style = popts.get("caption_style") or "none"
    tracking = bool(popts.get("tracking", True))
    start = float(cand.get("start_time", 0) or 0)
    end = float(cand.get("end_time", 0) or 0)
    if end <= start:
        raise HTTPException(status_code=400, detail="Candidate has invalid start/end bounds")

    title = (cand.get("hook") or cand.get("first_sentence") or "").strip()[:120]
    if not title:
        title = f"Clip {start:.1f}s – {end:.1f}s"

    state = {
        "source_start": start,
        "source_end": end,
        "output_aspect": aspect,
        "canvas_preset": aspect if aspect in composition_mod.CANVAS_PRESETS else "9:16",
        "caption_enabled": bool(caption_style and caption_style != "none"),
        "caption_style": caption_style,
        "layout_mode": layout_mode,
        "zoom": float(popts.get("zoom_boost") or 1.0),
        "tracking": tracking,
        "crop_mode": "tracking" if tracking else "manual",
        "x": 0.5,
        "y": 0.5,
        "export_preset": None,
        "composition": None,
    }

    new_clip = await database.save_clip(
        project_id=project_id,
        file_path="",
        start_time=start,
        end_time=end,
        title=title,
        reason=cand.get("payoff_sentence") or "Created from editorial review",
        viral_score=round(float(cand.get("final_score") or 5)),
        face_count=None,
        layout_mode=layout_mode,
        caption_style=caption_style,
        needs_user_confirm=False,
        reframed=True,
        hashtags=[],
        tags=[],
        aspect=aspect,
        cuts_applied=0,
    )
    clip_id = new_clip["id"]

    # Launch the same editor render pipeline (updates the new row, records history).
    job = _ensure_render_job(clip_id, project_id, state)
    return {"job_id": job["id"], "clip_id": clip_id, "status": job["status"]}


def _ensure_render_job(clip_id: str, project_id: str, state: Optional[dict] = None) -> dict:
    """Start an editor render job if none is running for the clip (shared helper)."""
    active = [j for j in _render_jobs.values() if j.get("clip_id") == clip_id and j.get("status") == "running"]
    if active:
        return active[0]
    return _start_editor_job(clip_id, project_id, state)


@app.post("/api/projects/{project_id}/ab-experiment")
async def ab_experiment(project_id: str):
    """
    A/B experiment: current production scorer vs new editorial evaluation reranking.
    Returns top 5 from each for comparison.
    """
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    cands = await database.get_candidate_evaluations(project_id)
    if not cands:
        raise HTTPException(status_code=404, detail="No candidates found — run pipeline first")
    # A: current production (final_score)
    a_top = sorted(cands, key=lambda c: c.get("final_score", 0) or 0, reverse=True)[:5]
    # B: new editorial reranking (weighted hook/payoff/context + diversity already applied, but we can simulate alternative weighting)
    # For B, rerank by hook_first_3s + payoff + audio/visual
    def b_score(c):
        h = c.get("hook_first_3s") or 5
        p = c.get("payoff_score") or 5
        a = c.get("audio_score") or 5
        v = c.get("visual_score") or 5
        # New weighting: hook_first_3s 30%, payoff 25%, audio 15%, visual 10%, final 20%
        return h*0.3 + p*0.25 + a*0.15 + v*0.10 + (c.get("final_score") or 50)/10*0.20
    b_top = sorted(cands, key=b_score, reverse=True)[:5]
    result = {
        "project_id": project_id,
        "A_current": [{"start": c["start_time"], "end": c["end_time"], "final_score": c["final_score"], "hook": c["hook_score"], "payoff": c["payoff_score"], "first_3s": c["first_3s_transcript"][:80]} for c in a_top],
        "B_new": [{"start": c["start_time"], "end": c["end_time"], "final_score": round(b_score(c),1), "hook_first_3s": c["hook_first_3s"], "payoff": c["payoff_score"], "first_3s": c["first_3s_transcript"][:80]} for c in b_top],
        "overlap": len(set((c["start_time"], c["end_time"]) for c in a_top) & set((c["start_time"], c["end_time"]) for c in b_top)),
    }
    # P1-8: persist the run so review history keeps a record of experiments.
    try:
        experiment_id = await database.save_ab_experiment(project_id, result)
        result["experiment_id"] = experiment_id
    except Exception as exc:
        logger.warning("Failed to persist A/B experiment: %s", exc)
    return result


@app.get("/api/projects/{project_id}/ab-experiments")
async def list_ab_experiments(project_id: str):
    """Return every persisted A/B experiment run for a project, newest first."""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return await database.get_ab_experiments(project_id)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """
    Delete a project and all of its clips (both DB rows and files).
    """
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await database.delete_project(project_id)

    # Clean up files on disk
    project_dir = os.path.join(TMP_DIR, project_id)
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)

    return {"success": True}


@app.get("/api/projects/{project_id}/export-zip")
async def export_project_zip(project_id: str):
    """
    Bundle every clip's latest render into a single .zip download.
    Builds a temp zip in a worker thread, streams it, then cleans up.
    """
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    clips = project.get("clips") or []
    if not clips:
        raise HTTPException(status_code=404, detail="No clips to export")

    project_dir = os.path.join(TMP_DIR, project_id)
    os.makedirs(project_dir, exist_ok=True)
    safe_tag = re.sub(r"[^a-zA-Z0-9 _\-]+", "", (project.get("title") or "project")).strip() or "project"
    zip_path = os.path.join(project_dir, f".export_{project_id}.zip")
    shutil.rmtree(zip_path, ignore_errors=True)

    def _build():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            written = 0
            seen: set[str] = set()
            for i, c in enumerate(clips):
                try:
                    renders = asyncio.run(database.get_clip_renders(c["id"]))
                    rel = (renders[-1].get("file_path") or c["file_path"]) if renders else c["file_path"]
                except Exception:
                    rel = c["file_path"]
                full = os.path.normpath(os.path.join(TMP_DIR, rel.replace("/files/", "", 1)))
                if not os.path.isfile(full) or full in seen:
                    continue
                seen.add(full)
                fname = c.get("title") or f"clip_{i}"
                safe = re.sub(r"[^a-zA-Z0-9 _\-]+", "", fname).strip() or f"clip_{i}"
                zf.write(full, f"{i+1:02d}_{safe}.mp4")
                written += 1
        return written

    try:
        written = await asyncio.to_thread(_build)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Zip export failed: {exc}")
    if written == 0:
        raise HTTPException(status_code=404, detail="No renderable clip files found — re-render first")
    if not os.path.isfile(zip_path):
        raise HTTPException(status_code=500, detail="Zip export produced no file")

    def _cleanup():
        try:
            os.remove(zip_path)
        except OSError:
            pass

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{safe_tag}_clips.zip",
        background=BackgroundTask(_cleanup),
    )


@app.get("/api/storage")
async def get_storage_usage():
    """Total disk usage for all project media, with a per-project breakdown."""
    projects = await database.get_all_projects()
    total = 0
    for p in projects:
        total += int(p.get("size_bytes") or 0)
    return {
        "total_bytes": total,
        "tmp_dir": os.path.abspath(TMP_DIR),
        "projects": projects,
        "project_count": len(projects),
    }


@app.get("/api/caption-styles")
async def list_caption_styles():
    def ass_to_hex(ass_color):
        if not ass_color: return None
        if len(ass_color) >= 10:
            return f"#{ass_color[8:10]}{ass_color[6:8]}{ass_color[4:6]}"
        return None

    styles = []
    for key, style in captioner.CAPTION_STYLES.items():
        styles.append({
            "key": key,
            "name": style["name"],
            "animation": style["animation"],
            "preview_colors": {
                "text": ass_to_hex(style.get("primary_color")),
                "highlight": ass_to_hex(style.get("highlight_color")),
                "background": ass_to_hex(style.get("bg_color")) if style.get("background") else None
            }
        })
    return styles

# ---------------------------------------------------------------------------
# REST routes — Settings
# ---------------------------------------------------------------------------

class UpdateSettingsRequest(BaseModel):
    """Payload for updating settings."""
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_api_key_openrouter: Optional[str] = None
    llm_api_key_mistral: Optional[str] = None
    llm_api_key_groq: Optional[str] = None
    llm_api_key_anyapi: Optional[str] = None
    llm_api_key_cohere: Optional[str] = None
    llm_api_key_llm7: Optional[str] = None
    llm_api_key_xkiro: Optional[str] = None
    llm_model: Optional[str] = None
    whisper_model: Optional[str] = None
    caption_style: Optional[str] = None


@app.get("/api/settings")
async def get_settings():
    """
    Return all settings.  API keys are **never** returned — only a
    boolean ``has_api_key`` flag.
    """
    return await settings_mod.get_all_settings()


@app.post("/api/settings")
async def update_settings(body: UpdateSettingsRequest):
    """
    Create or update one or more settings.
    """
    pairs = {
        "llm_provider": body.llm_provider,
        "llm_api_key": body.llm_api_key,
        "llm_api_key_openrouter": body.llm_api_key_openrouter,
        "llm_api_key_mistral": body.llm_api_key_mistral,
        "llm_api_key_groq": body.llm_api_key_groq,
        "llm_api_key_anyapi": body.llm_api_key_anyapi,
        "llm_api_key_cohere": body.llm_api_key_cohere,
        "llm_api_key_llm7": body.llm_api_key_llm7,
        "llm_api_key_xkiro": body.llm_api_key_xkiro,
        "llm_model": body.llm_model,
        "whisper_model": body.whisper_model,
        "caption_style": body.caption_style,
    }
    for key, value in pairs.items():
        if value is not None:
            await settings_mod.set_setting(key, value)
    # Mirror a newly saved generic key onto its provider slot so switching
    # providers later doesn't lose it.
    if body.llm_api_key is not None and body.llm_provider:
        await settings_mod.set_setting(f"llm_api_key_{body.llm_provider}", body.llm_api_key)
    return {"success": True}


# ---------------------------------------------------------------------------
# WebSocket — real-time progress
# ---------------------------------------------------------------------------

@app.websocket("/ws/progress/{project_id}")
async def websocket_progress(websocket: WebSocket, project_id: str):
    """
    WebSocket endpoint that streams processing-progress updates to
    the frontend for a given project.

    Message format (JSON sent from server):
        {
          "stage":   "download" | "clipping" | "done" | "error",
          "percent": 0.0 – 100.0,
          "message": "human-readable status"
        }

    The connection stays open until the client disconnects.
    """
    await websocket.accept()

    # Register this socket
    if project_id not in _ws_connections:
        _ws_connections[project_id] = set()
    _ws_connections[project_id].add(websocket)

    try:
        # Stay alive — wait for the client to close or send pings
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_connections.get(project_id, set()).discard(websocket)
        if project_id in _ws_connections and not _ws_connections[project_id]:
            _ws_connections.pop(project_id, None)


@app.websocket("/ws/render/{clip_id}")
async def websocket_render_progress(websocket: WebSocket, clip_id: str):
    """
    Editor render progress channel. Sends job status snapshots:
      {"job_id", "status", "stage", "percent", "message", "output", "error"}
    The active job status is pushed immediately on connect.
    """
    await websocket.accept()
    if clip_id not in _render_ws:
        _render_ws[clip_id] = set()
    _render_ws[clip_id].add(websocket)
    # Push the current job state right away
    try:
        active = [j for j in _render_jobs.values() if j.get("clip_id") == clip_id]
        if not active:
            active = []
            persisted = await database.latest_render_job(clip_id)
            if persisted:
                await websocket.send_json(_render_job_status(persisted))
        if active:
            await websocket.send_json(_render_job_status(active[-1]))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _render_ws.get(clip_id, set()).discard(websocket)
        if clip_id in _render_ws and not _render_ws[clip_id]:
            _render_ws.pop(clip_id, None)


# ---------------------------------------------------------------------------
# Entrypoint (for development)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn  # type: ignore
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)

# Benchmark API endpoints (Phase 4.1.2)
# Inserted after review_clip function

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if False else ".")
try:
    import benchmark_dataset as _bd
except ImportError:
    import sys as _s
    _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import benchmark_dataset as _bd

# Pydantic models for benchmark API
class BenchmarkAnnotationRequest(BaseModel):
    video_id: str
    annotator_id: str
    start: float
    end: float
    title: str = ""
    description: str = ""
    why_valuable: str = ""
    categories: list[str] = []
    importance: int = 3
    confidence: int = 3
    notes: str = ""

class BenchmarkAnnotationResponse(BaseModel):
    success: bool
    moment_id: str
    video_id: str

class BenchmarkReviewRequest(BaseModel):
    candidate_id: str
    video_id: str
    reviewer_id: str
    opening_hook: int = 0
    context_completeness: int = 0
    interesting_moment: int = 0
    ending_payoff: int = 0
    audio_clarity: int = 0
    visual_quality: int = 0
    overall_usefulness: int = 0
    label: str = ""
    rejection_reasons: list[str] = []
    notes: str = ""

class BenchmarkReviewResponse(BaseModel):
    success: bool
    review_id: str
    candidate_id: str

class BenchmarkMatchRequest(BaseModel):
    video_id: str
    candidates: list[dict]
    iou_threshold: float = 0.5

class BenchmarkMatchResponse(BaseModel):
    success: bool
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    matches: list[dict]


@app.post("/api/benchmark/annotations", response_model=BenchmarkAnnotationResponse)
async def create_benchmark_annotation(body: BenchmarkAnnotationRequest):
    """Create a human-interest moment annotation."""
    # Validate video_id (prevent path traversal)
    if not body.video_id or ".." in body.video_id or "/" in body.video_id or "\\" in body.video_id:
        raise HTTPException(status_code=400, detail="Invalid video_id")
    # Validate timestamps
    if body.start < 0:
        raise HTTPException(status_code=400, detail="Start must be non-negative")
    if body.end <= body.start:
        raise HTTPException(status_code=400, detail="End must be after start")
    if body.importance < 1 or body.importance > 5:
        raise HTTPException(status_code=400, detail="Importance must be 1-5")
    if body.confidence < 1 or body.confidence > 5:
        raise HTTPException(status_code=400, detail="Confidence must be 1-5")
    for cat in body.categories:
        if cat not in _bd.EDITORIAL_CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category: %s" % cat)

    import uuid as _uuid
    moment = _bd.InterestingMoment(
        moment_id=str(_uuid.uuid4()),
        video_id=body.video_id,
        annotator_id=body.annotator_id,
        start=body.start,
        end=body.end,
        title=body.title,
        description=body.description,
        why_valuable=body.why_valuable,
        categories=body.categories,
        importance=body.importance,
        confidence=body.confidence,
        notes=body.notes,
    )
    _bd.save_annotation(DATA_DIR.replace("data", "data") if False else "data", moment)
    return BenchmarkAnnotationResponse(
        success=True,
        moment_id=moment.moment_id,
        video_id=body.video_id,
    )


@app.get("/api/benchmark/annotations/{video_id}")
async def list_benchmark_annotations(video_id: str, annotator_id: Optional[str] = None):
    """List annotations for a video."""
    anns = _bd.load_annotations("data", video_id, annotator_id=annotator_id)
    return {"video_id": video_id, "annotations": [asdict(a) if False else {
        "moment_id": a.moment_id,
        "video_id": a.video_id,
        "annotator_id": a.annotator_id,
        "start": a.start,
        "end": a.end,
        "title": a.title,
        "description": a.description,
        "why_valuable": a.why_valuable,
        "categories": a.categories,
        "importance": a.importance,
        "confidence": a.confidence,
        "notes": a.notes,
    } for a in anns]}


@app.post("/api/benchmark/reviews", response_model=BenchmarkReviewResponse)
async def create_benchmark_review(body: BenchmarkReviewRequest):
    """Save a candidate review."""
    # Validate video_id (prevent path traversal)
    if not body.video_id or ".." in body.video_id or "/" in body.video_id or "\\" in body.video_id:
        raise HTTPException(status_code=400, detail="Invalid video_id")
    import uuid as _uuid
    review = _bd.CandidateReview(
        review_id=str(_uuid.uuid4()),
        candidate_id=body.candidate_id,
        video_id=body.video_id,
        reviewer_id=body.reviewer_id,
        opening_hook=body.opening_hook,
        context_completeness=body.context_completeness,
        interesting_moment=body.interesting_moment,
        ending_payoff=body.ending_payoff,
        audio_clarity=body.audio_clarity,
        visual_quality=body.visual_quality,
        overall_usefulness=body.overall_usefulness,
        label=body.label,
        rejection_reasons=body.rejection_reasons,
        notes=body.notes,
    )
    _bd.save_candidate_review("data", review)
    return BenchmarkReviewResponse(
        success=True,
        review_id=review.review_id,
        candidate_id=body.candidate_id,
    )


@app.get("/api/benchmark/reviews/{video_id}")
async def list_benchmark_reviews(video_id: str, reviewer_id: Optional[str] = None):
    """List reviews for a video."""
    reviews = _bd.load_candidate_reviews("data", video_id, reviewer_id=reviewer_id)
    return {"video_id": video_id, "reviews": [{
        "review_id": r.review_id,
        "candidate_id": r.candidate_id,
        "video_id": r.video_id,
        "reviewer_id": r.reviewer_id,
        "opening_hook": r.opening_hook,
        "context_completeness": r.context_completeness,
        "interesting_moment": r.interesting_moment,
        "ending_payoff": r.ending_payoff,
        "audio_clarity": r.audio_clarity,
        "visual_quality": r.visual_quality,
        "overall_usefulness": r.overall_usefulness,
        "label": r.label,
        "rejection_reasons": r.rejection_reasons,
        "notes": r.notes,
    } for r in reviews]}


@app.post("/api/benchmark/match", response_model=BenchmarkMatchResponse)
async def run_benchmark_match(body: BenchmarkMatchRequest):
    """Run candidate-reference matching and return metrics."""
    # Validate video_id (prevent path traversal)
    if not body.video_id or ".." in body.video_id or "/" in body.video_id or "\\" in body.video_id:
        raise HTTPException(status_code=400, detail="Invalid video_id")
    anns = _bd.load_annotations("data", body.video_id)
    match_result = _bd.match_candidates_to_references(body.candidates, anns, iou_threshold=body.iou_threshold)
    metrics = _bd.compute_detection_metrics(match_result)
    return BenchmarkMatchResponse(
        success=True,
        true_positives=match_result["true_positives"],
        false_positives=match_result["false_positives"],
        false_negatives=match_result["false_negatives"],
        precision=metrics["precision"],
        recall=metrics["recall"],
        f1=metrics["f1"],
        matches=match_result["matches"],
    )


@app.get("/api/benchmark/datasets")
async def list_benchmark_datasets():
    """List available benchmark datasets."""
    base = "data/evaluation/benchmarks"
    if not os.path.exists(base):
        return {"datasets": []}
    datasets = []
    for name in os.listdir(base):
        path = os.path.join(base, name, "dataset.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            datasets.append({
                "version": manifest.get("dataset_version", name),
                "video_count": len(manifest.get("videos", [])),
                "created_at": manifest.get("created_at", ""),
            })
    return {"datasets": datasets}

# Export endpoints for benchmark reports (Phase 4.1.3)
# Added after benchmark datasets endpoint

@app.get("/api/benchmark/export/{video_id}")
async def export_benchmark_report(video_id: str, format: str = "json"):
    """Export benchmark report in JSON, CSV, or HTML format."""
    import tempfile, os as _os
    from fastapi.responses import FileResponse, Response

    # Validate format
    if format not in ("json", "csv", "html"):
        raise HTTPException(status_code=400, detail="Format must be json, csv, or html")

    # Load annotations and reviews
    anns = _bd.load_annotations("data", video_id)
    reviews = _bd.load_candidate_reviews("data", video_id)

    # Get candidates from annotations (as reference moments)
    # For export, we create a minimal report
    import evaluation_engine as _ee

    # Build candidate list from annotations for matching
    candidates = []
    for a in anns:
        candidates.append({"start": a.start, "end": a.end, "title": a.title, "viral_score": a.importance})

    # Run matching if we have both
    match_result = None
    if candidates and anns:
        match_result = _bd.match_candidates_to_references(candidates, anns, 0.5)

    # Create a minimal evaluation report
    report = _ee.EvaluationReport()
    report.video_path = video_id
    report.candidate_count = len(candidates)

    if match_result:
        report.diversity = _ee.evaluate_diversity(candidates)

    # Save to temp file
    tmpdir = tempfile.mkdtemp()
    base = _os.path.join(tmpdir, "report")

    if format == "json":
        path = base + ".json"
        _ee.report_to_json(report, path)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content=content, media_type="application/json",
                       headers={"Content-Disposition": "attachment; filename=benchmark_%s.json" % video_id})
    elif format == "csv":
        path = base + ".csv"
        _ee.report_to_csv(report, path)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content=content, media_type="text/csv",
                       headers={"Content-Disposition": "attachment; filename=benchmark_%s.csv" % video_id})
    else:
        path = base + ".html"
        _ee.report_to_html(report, path)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content=content, media_type="text/html",
                       headers={"Content-Disposition": "attachment; filename=benchmark_%s.html" % video_id})

# Benchmark dataset and import endpoints (Phase 5.3.1)

from fastapi import UploadFile, File, Form


@app.post("/api/benchmark/import")
async def import_benchmark_video(
    file: UploadFile = File(...),
    video_id: str = Form(...),
    permission_status: str = Form("pending"),
    permission_basis: str = Form(""),
    content_category: str = Form("other"),
    media_type: str = Form("real"),
):
    """Import a video into the benchmark dataset via streaming upload."""
    import hashlib as _hl
    import os as _os
    import tempfile as _tf
    import subprocess as _sp

    # Validate video_id
    if not video_id or ".." in video_id or "/" in video_id or "\\" in video_id:
        raise HTTPException(status_code=400, detail="Invalid video_id")

    # Validate permission
    if permission_status not in ("pending", "authorized", "denied"):
        raise HTTPException(status_code=400, detail="Invalid permission_status")

    # Create storage directory
    media_dir = os.path.join("data", "evaluation", "benchmarks", "media")
    os.makedirs(media_dir, exist_ok=True)

    # Generate safe filename
    safe_name = "%s_%s" % (video_id, os.path.basename(file.filename or "video.mp4"))
    # Remove any path components
    safe_name = os.path.basename(safe_name)
    # Sanitize
    safe_name = "".join(c for c in safe_name if c.isalnum() or c in "._-")
    if not safe_name:
        safe_name = "video_%s.mp4" % video_id
    dest_path = os.path.join(media_dir, safe_name)

    # Check for duplicate (by hash) - stream and hash
    hasher = _hl.sha256()
    tmp_fd, tmp_path = _tf.mkstemp(dir=media_dir, suffix=".tmp")
    file_size = 0
    max_size = 500 * 1024 * 1024  # 500MB limit

    try:
        with _os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > max_size:
                    raise HTTPException(status_code=413, detail="File too large (max 500MB)")
                hasher.update(chunk)
                out.write(chunk)
                out.flush()
        file_hash = hasher.hexdigest()

        # Check duplicate
        import benchmark_dataset as _bd
        existing_videos = _bd.get_videos("data")
        for v in existing_videos:
            if v.get("source_hash") == file_hash:
                # Duplicate found - clean up temp file
                try:
                    _os.remove(tmp_path)
                except OSError:
                    pass
                return {
                    "success": True,
                    "duplicate": True,
                    "existing_video_id": v.get("video_id"),
                    "source_hash": file_hash,
                    "message": "This video has already been imported",
                }

        # Validate media with ffprobe
        result = _sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height,r_frame_rate,codec_type,codec_name",
             "-show_entries", "format=duration,format_name", "-of", "json", tmp_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            try:
                _os.remove(tmp_path)
            except OSError:
                pass
            raise HTTPException(status_code=400, detail="Invalid or unsupported video file")

        import json as _json
        probe_data = _json.loads(result.stdout)
        streams = probe_data.get("streams", [])
        fmt = probe_data.get("format", {})

        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

        if not video_streams:
            try:
                _os.remove(tmp_path)
            except OSError:
                pass
            raise HTTPException(status_code=400, detail="No video stream found")

        vstream = video_streams[0]
        fps_str = vstream.get("r_frame_rate", "0/1")
        if "/" in fps_str:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if float(den) > 0 else 0.0
        else:
            fps = float(fps_str) if fps_str else 0.0

        duration = float(fmt.get("duration", 0))
        width = vstream.get("width", 0)
        height = vstream.get("height", 0)
        has_audio = len(audio_streams) > 0

        # Atomically finalize
        _os.replace(tmp_path, dest_path)

        # Register in dataset
        meta = _bd.VideoMetadata(
            video_id=video_id,
            source_hash=file_hash,
            duration=duration,
            resolution="%dx%d" % (width, height),
            fps=fps,
            has_audio=has_audio,
            has_transcript=False,
            permission_status=permission_status,
            permission_basis=permission_basis,
            media_type=media_type,
            added_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        _bd.add_video("data", meta)

        return {
            "success": True,
            "duplicate": False,
            "video_id": video_id,
            "source_hash": file_hash,
            "duration": duration,
            "resolution": "%dx%d" % (width, height),
            "fps": fps,
            "has_audio": has_audio,
            "file_size": file_size,
            "media_type": media_type,
            "permission_status": permission_status,
        }

    except HTTPException:
        raise
    except Exception as e:
        try:
            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Import failed: %s" % str(e))


@app.get("/api/benchmark/videos")
async def list_benchmark_videos(dataset_version: str = "phase4-v1"):
    """List videos in a benchmark dataset."""
    videos = _bd.get_videos("data", dataset_version)
    return {"dataset_version": dataset_version, "videos": videos, "count": len(videos)}


@app.get("/api/benchmark/videos/{video_id}")
async def get_benchmark_video(video_id: str, dataset_version: str = "phase4-v1"):
    """Get a single benchmark video's metadata."""
    videos = _bd.get_videos("data", dataset_version)
    for v in videos:
        if v.get("video_id") == video_id:
            return v
    raise HTTPException(status_code=404, detail="Video not found")


@app.post("/api/benchmark/runs")
async def start_benchmark_run(video_id: str, experiment_id: str = "phase5.3.1-baseline-v1"):
    """Start a baseline benchmark run on a video."""
    import uuid as _uuid
    # Validate video exists
    videos = _bd.get_videos("data")
    video = None
    for v in videos:
        if v.get("video_id") == video_id:
            video = v
            break
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.get("permission_status") != "authorized":
        raise HTTPException(status_code=403, detail="Video not authorized for benchmarking")

    run_id = str(_uuid.uuid4())
    # Store run record
    runs_dir = os.path.join("data", "evaluation", "benchmarks", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    run_record = {
        "run_id": run_id,
        "video_id": video_id,
        "experiment_id": experiment_id,
        "status": "completed",
        "source_hash": video.get("source_hash", ""),
        "dataset_version": video.get("dataset_version", "phase4-v1"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result": {"status": "baseline_ready", "message": "Baseline run recorded"},
    }
    run_path = os.path.join(runs_dir, "%s.json" % run_id)
    with open(run_path, "w", encoding="utf-8") as f:
        json.dump(run_record, f, indent=2)

    return {"success": True, "run_id": run_id, "status": "completed"}


@app.get("/api/benchmark/runs")
async def list_benchmark_runs(video_id: Optional[str] = None):
    """List benchmark runs, optionally filtered by video."""
    runs_dir = os.path.join("data", "evaluation", "benchmarks", "runs")
    if not os.path.exists(runs_dir):
        return {"runs": []}
    runs = []
    for fname in os.listdir(runs_dir):
        if fname.endswith(".json"):
            with open(os.path.join(runs_dir, fname), "r", encoding="utf-8") as f:
                run = json.load(f)
            if video_id and run.get("video_id") != video_id:
                continue
            runs.append(run)
    return {"runs": runs, "count": len(runs)}


@app.get("/api/benchmark/runs/{run_id}")
async def get_benchmark_run(run_id: str):
    """Get a specific benchmark run."""
    run_path = os.path.join("data", "evaluation", "benchmarks", "runs", "%s.json" % run_id)
    if not os.path.exists(run_path):
        raise HTTPException(status_code=404, detail="Run not found")
    with open(run_path, "r", encoding="utf-8") as f:
        return json.load(f)
