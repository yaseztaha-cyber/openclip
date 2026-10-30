"""
database.py — Async SQLite database layer for OpenClip.

Uses aiosqlite to provide non-blocking database access.  Manages the
connection to the local SQLite database and provides CRUD helpers for
the `projects` and `clips` tables.

How it works:
    Every public function opens a short-lived aiosqlite connection, runs
    its query, and closes the connection.  This keeps the API simple and
    avoids long-lived connection state.  WAL journal mode and foreign
    keys are enabled on every connection for performance and integrity.
"""

import aiosqlite  # type: ignore
import uuid
import os
import json
from typing import Optional
from dotenv import load_dotenv  # type: ignore

load_dotenv()


# ---------------------------------------------------------------------------
# Path to the SQLite database file (relative to project root)
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "openclip.db"))


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

async def _get_connection() -> aiosqlite.Connection:
    """Return an async SQLite connection with row-factory enabled."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """
    Create the `projects`, `clips`, and `settings` tables if they do not
    exist.  Also runs lightweight migrations (e.g. adding columns) so
    that upgrades are seamless.

    Called once at application startup to guarantee the schema is ready
    before any request arrives.
    """
    conn = await _get_connection()
    try:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id          TEXT PRIMARY KEY,
                youtube_url TEXT NOT NULL,
                title       TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS clips (
                id          TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                file_path   TEXT NOT NULL,
                start_time  REAL NOT NULL,
                end_time    REAL NOT NULL,
                duration    REAL NOT NULL,
                reframed    BOOLEAN NOT NULL DEFAULT 1,
                captioned   BOOLEAN NOT NULL DEFAULT 0,
                title       TEXT,
                reason      TEXT,
                viral_score INTEGER,
                face_count  INTEGER,
                layout_mode TEXT,
                caption_style TEXT,
                needs_user_confirm BOOLEAN NOT NULL DEFAULT 0,
                hashtags TEXT,
                tags TEXT,
                aspect TEXT NOT NULL DEFAULT '9:16',
                final_score REAL,
                hook_score REAL,
                hook_first_3s_score REAL,
                payoff_score REAL,
                audio_score REAL,
                visual_score REAL,
                reaction_score REAL,
                pacing_score REAL,
                cuts_applied INTEGER DEFAULT 0,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS clip_editor_state (
                clip_id    TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS candidate_evaluations (
                id          TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                start_time  REAL NOT NULL,
                end_time    REAL NOT NULL,
                duration    REAL NOT NULL,
                hook_score  REAL,
                hook_first_3s REAL,
                payoff_score REAL,
                curiosity   REAL,
                information REAL,
                narrative   REAL,
                audio_score REAL,
                visual_score REAL,
                reaction_score REAL,
                pacing_score REAL,
                final_score REAL,
                first_3s_transcript TEXT,
                first_sentence TEXT,
                payoff_sentence TEXT,
                final_sentence TEXT,
                failure_reasons TEXT, -- JSON array
                hook_detail TEXT, -- JSON
                payoff_detail TEXT,
                context_detail TEXT,
                selected    INTEGER DEFAULT 0,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS clip_reviews (
                id          TEXT PRIMARY KEY,
                clip_id     TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                decision    TEXT NOT NULL, -- KEEP / EDIT / REJECT
                reason      TEXT, -- Weak hook / No payoff / etc.
                notes       TEXT,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS clip_renders (
                id          TEXT PRIMARY KEY,
                clip_id     TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                file_path   TEXT NOT NULL,
                output_hash TEXT,
                mode        TEXT NOT NULL DEFAULT 'reframe',
                canvas      TEXT,
                composition_json TEXT,
                render_settings_json TEXT,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS render_jobs (
                id               TEXT PRIMARY KEY,
                clip_id          TEXT,
                project_id       TEXT,
                kind             TEXT NOT NULL DEFAULT 'clip',
                status           TEXT NOT NULL DEFAULT 'queued',
                stage            TEXT,
                percent          REAL NOT NULL DEFAULT 0,
                message          TEXT,
                error            TEXT,
                output           TEXT,
                clips_status_json TEXT,
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at      DATETIME
            );

            CREATE TABLE IF NOT EXISTS ab_experiments (
                id          TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            """
        )
        await conn.commit()

        # --- Migrations: add columns if they don't exist yet -----------
        # --- Migration: project-level pipeline adjustments ---------------
        try:
            await conn.execute("ALTER TABLE projects ADD COLUMN pipeline_options TEXT")
            await conn.commit()
        except Exception:
            pass  # column already exists

        for col in ("title", "reason", "viral_score", "face_count", "layout_mode", "caption_style", "needs_user_confirm", "hashtags", "tags", "aspect",
                    "final_score", "hook_score", "hook_first_3s_score", "payoff_score", "audio_score", "visual_score", "reaction_score", "pacing_score", "cuts_applied"):
            try:
                if col in ("viral_score", "face_count", "cuts_applied"):
                    col_type = "INTEGER"
                elif col in ("final_score", "hook_score", "hook_first_3s_score", "payoff_score", "audio_score", "visual_score", "reaction_score", "pacing_score"):
                    col_type = "REAL"
                elif col == "needs_user_confirm":
                    col_type = "BOOLEAN"
                else:
                    col_type = "TEXT"
                await conn.execute(
                    f"ALTER TABLE clips ADD COLUMN {col} {col_type}"
                )
                await conn.commit()
            except Exception:
                pass  # column already exists — safe to ignore

    except Exception as exc:
        raise RuntimeError(f"Failed to initialise database: {exc}") from exc
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

async def create_project(youtube_url: str, pipeline_options: Optional[dict] = None) -> dict:
    """
    Insert a new project row and return its id + initial status.

    Args:
        youtube_url:       The YouTube video URL to associate with this project.
        pipeline_options:  Optional per-project framing adjustments
                           (aspect/layout_mode/zoom/tracking/caption_style)
                           applied to every auto-cut clip.

    Returns:
        dict with ``project_id`` and ``status`` keys.
    """
    project_id = str(uuid.uuid4())
    pipeline_json = json.dumps(pipeline_options or {})
    conn = await _get_connection()
    try:
        await conn.execute(
            "INSERT INTO projects (id, youtube_url, pipeline_options) VALUES (?, ?, ?)",
            (project_id, youtube_url, pipeline_json),
        )
        await conn.commit()
        ret = {"project_id": project_id, "status": "pending"}
    except Exception as exc:
        raise RuntimeError(f"Failed to create project: {exc}") from exc
    finally:
        await conn.close()
    return ret


async def update_project_status(project_id: str, status: str) -> None:
    """
    Update the status field of a project.

    Args:
        project_id: UUID of the project.
        status:     New status value (pending/downloading/processing/done/error).
    """
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (status, project_id),
        )
        await conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to update project status: {exc}") from exc
    finally:
        await conn.close()


async def reset_stale_projects() -> int:
    """
    Mark any project left in an active state as errored.

    Pipelines run as in-memory background tasks, so a backend restart
    orphans them: the DB row stays e.g. "processing" forever with no
    worker. Call this on startup so stuck projects fail visibly instead
    of hanging at 0% with a dead progress bar.

    Returns:
        Number of projects reset.
    """
    conn = await _get_connection()
    try:
        cursor = await conn.execute(
            "UPDATE projects SET status = 'error' "
            "WHERE status NOT IN ('done', 'error')"
        )
        await conn.commit()
        return cursor.rowcount or 0
    except Exception as exc:
        raise RuntimeError(f"Failed to reset stale projects: {exc}") from exc
    finally:
        await conn.close()


async def update_project_title(project_id: str, title: str) -> None:
    """
    Update the title field of a project.

    Args:
        project_id: UUID of the project.
        title:      New title string.
    """
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (title, project_id),
        )
        await conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to update project title: {exc}") from exc
    finally:
        await conn.close()


async def get_all_projects() -> list[dict]:
    """
    Return every project, newest first, with a ``clip_count`` field.

    The clip count is computed via a LEFT JOIN on the clips table so
    that projects with zero clips are still returned.

    Returns:
        List of project dicts, each containing:
        id, title, status, created_at, clip_count, size_bytes.
    """
    conn = await _get_connection()
    try:
        cursor = await conn.execute(
            """
            SELECT p.id, p.title, p.youtube_url, p.status, p.created_at,
                   COUNT(c.id) AS clip_count
            FROM projects p
            LEFT JOIN clips c ON c.project_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """
        )
        rows = await cursor.fetchall()
        ret = [
            {
                "id": row["id"],
                "title": row["title"],
                "youtube_url": row["youtube_url"],
                "status": row["status"],
                "created_at": row["created_at"],
                "clip_count": row["clip_count"],
                "size_bytes": _project_dir_size(row["id"]),
            }
            for row in rows
        ]
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch projects: {exc}") from exc
    finally:
        await conn.close()
    return ret


async def get_project(project_id: str) -> Optional[dict]:
    """
    Return a single project by ID, including its list of clips.

    Args:
        project_id: UUID of the project to fetch.

    Returns:
        A dict with project fields plus a ``clips`` list, or *None* if
        the project does not exist.
    """
    conn = await _get_connection()
    try:
        # Fetch project row
        cursor = await conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None

        project = {
            "id": row["id"],
            "title": row["title"],
            "youtube_url": row["youtube_url"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
        if "pipeline_options" in row.keys() and row["pipeline_options"]:
            try:
                project["pipeline_options"] = json.loads(row["pipeline_options"])
            except Exception:
                project["pipeline_options"] = {}
        else:
            project["pipeline_options"] = {}

        # Fetch associated clips
        cursor = await conn.execute(
            "SELECT * FROM clips WHERE project_id = ? ORDER BY start_time",
            (project_id,),
        )
        clip_rows = await cursor.fetchall()
        project["clips"] = [
            {
                "id": c["id"],
                "project_id": c["project_id"],
                "file_path": c["file_path"],
                "start_time": c["start_time"],
                "end_time": c["end_time"],
                "duration": c["duration"],
                "reframed": bool(c["reframed"]),
                "captioned": bool(c["captioned"]),
                "title": c["title"],
                "reason": c["reason"],
                "viral_score": c["viral_score"],
                "face_count": c["face_count"],
                "layout_mode": c["layout_mode"] if "layout_mode" in c.keys() else "none",
                "aspect": c["aspect"] if "aspect" in c.keys() and c["aspect"] else "9:16",
                "caption_style": c["caption_style"] if "caption_style" in c.keys() else "none",
                "needs_user_confirm": bool(c["needs_user_confirm"]) if "needs_user_confirm" in c.keys() else False,
                "hashtags": json.loads(c["hashtags"]) if "hashtags" in c.keys() and c["hashtags"] else [],
                "tags": json.loads(c["tags"]) if "tags" in c.keys() and c["tags"] else [],
                "final_score": c["final_score"] if "final_score" in c.keys() else None,
                "hook_score": c["hook_score"] if "hook_score" in c.keys() else None,
                "hook_first_3s_score": c["hook_first_3s_score"] if "hook_first_3s_score" in c.keys() else None,
                "payoff_score": c["payoff_score"] if "payoff_score" in c.keys() else None,
                "audio_score": c["audio_score"] if "audio_score" in c.keys() else None,
                "visual_score": c["visual_score"] if "visual_score" in c.keys() else None,
                "reaction_score": c["reaction_score"] if "reaction_score" in c.keys() else None,
                "pacing_score": c["pacing_score"] if "pacing_score" in c.keys() else None,
                "cuts_applied": c["cuts_applied"] if "cuts_applied" in c.keys() else None,
                "created_at": c["created_at"],
            }
            for c in clip_rows
        ]

        return project
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch project: {exc}") from exc
    finally:
        await conn.close()


async def save_clip(
    project_id: str,
    file_path: str,
    start_time: float,
    end_time: float,
    title: Optional[str] = None,
    reason: Optional[str] = None,
    viral_score: Optional[int] = None,
    face_count: Optional[int] = None,
    layout_mode: Optional[str] = None,
    caption_style: Optional[str] = None,
    needs_user_confirm: bool = False,
    reframed: bool = True,
    hashtags: Optional[list] = None,
    tags: Optional[list] = None,
    aspect: str = "9:16",
    final_score: Optional[float] = None,
    hook_score: Optional[float] = None,
    hook_first_3s_score: Optional[float] = None,
    payoff_score: Optional[float] = None,
    audio_score: Optional[float] = None,
    visual_score: Optional[float] = None,
    reaction_score: Optional[float] = None,
    pacing_score: Optional[float] = None,
    cuts_applied: Optional[int] = None,
) -> dict:
    """
    Insert a new clip row for a project.

    The duration is automatically computed as ``end_time - start_time``.

    Args:
        project_id: UUID of the parent project.
        file_path:  Absolute path to the clip file on disk.
        start_time: Clip start in seconds.
        end_time:   Clip end in seconds.
        title:      Optional clip title (from LLM suggestion).
        reason:     Optional reason why this clip was chosen.
        viral_score: Optional viral score out of 10.
        face_count: Optional max faces detected in the clip.
        reframed:   Whether the clip was reframed to 9:16.

    Returns:
        dict with the new clip's fields.
    """
    clip_id = str(uuid.uuid4())
    duration = end_time - start_time
    hashtags_json = json.dumps(hashtags) if hashtags else None
    tags_json = json.dumps(tags) if tags else None
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO clips
               (id, project_id, file_path, start_time, end_time, duration, title, reason, viral_score, face_count, layout_mode, caption_style, needs_user_confirm, reframed, hashtags, tags, aspect,
                final_score, hook_score, hook_first_3s_score, payoff_score, audio_score, visual_score, reaction_score, pacing_score, cuts_applied)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (clip_id, project_id, file_path, start_time, end_time, duration, title, reason, viral_score, face_count, layout_mode, caption_style, int(needs_user_confirm), int(reframed), hashtags_json, tags_json, aspect,
             final_score, hook_score, hook_first_3s_score, payoff_score, audio_score, visual_score, reaction_score, pacing_score, cuts_applied),
        )
        await conn.commit()
        ret = {
            "id": clip_id,
            "project_id": project_id,
            "file_path": file_path,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "reframed": reframed,
            "captioned": False,
            "title": title,
            "reason": reason,
            "viral_score": viral_score,
            "face_count": face_count,
            "layout_mode": layout_mode,
            "caption_style": caption_style,
            "needs_user_confirm": needs_user_confirm,
            "hashtags": hashtags or [],
            "tags": tags or [],
            "aspect": aspect,
            "final_score": final_score,
            "hook_score": hook_score,
            "hook_first_3s_score": hook_first_3s_score,
            "payoff_score": payoff_score,
            "audio_score": audio_score,
            "visual_score": visual_score,
            "reaction_score": reaction_score,
            "pacing_score": pacing_score,
            "cuts_applied": cuts_applied,
        }
    except Exception as exc:
        raise RuntimeError(f"Failed to save clip: {exc}") from exc
    finally:
        await conn.close()
    return ret


async def update_clip(
    clip_id: str,
    file_path: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    layout_mode: Optional[str] = None,
    caption_style: Optional[str] = None,
    face_count: Optional[int] = None,
    aspect: Optional[str] = None,
) -> None:
    """
    Update mutable fields of a clip row.

    Duration is recomputed when both start and end are supplied.
    """
    fields: list[str] = []
    values: list = []
    if file_path is not None:
        fields.append("file_path = ?")
        values.append(file_path)
    if start_time is not None:
        fields.append("start_time = ?")
        values.append(start_time)
    if end_time is not None:
        fields.append("end_time = ?")
        values.append(end_time)
    if start_time is not None and end_time is not None:
        fields.append("duration = ?")
        values.append(end_time - start_time)
    if layout_mode is not None:
        fields.append("layout_mode = ?")
        values.append(layout_mode)
    if caption_style is not None:
        fields.append("caption_style = ?")
        values.append(caption_style)
    if face_count is not None:
        fields.append("face_count = ?")
        values.append(face_count)
    if aspect is not None:
        fields.append("aspect = ?")
        values.append(aspect)
    if not fields:
        return
    values.append(clip_id)
    conn = await _get_connection()
    try:
        await conn.execute(
            f"UPDATE clips SET {', '.join(fields)} WHERE id = ?", values
        )
        await conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to update clip: {exc}") from exc
    finally:
        await conn.close()


async def delete_project(project_id: str) -> None:
    """
    Delete a project and all of its associated clips.

    Clips are deleted first to satisfy the foreign-key constraint,
    then the project row is removed.

    Args:
        project_id: UUID of the project to delete.
    """
    conn = await _get_connection()
    try:
        await conn.execute(
            "DELETE FROM clips WHERE project_id = ?", (project_id,)
        )
        await conn.execute(
            "DELETE FROM projects WHERE id = ?", (project_id,)
        )
        await conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to delete project: {exc}") from exc
    finally:
        await conn.close()

async def get_clip(clip_id: str) -> Optional[dict]:
    """Fetch a single clip by its ID."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        raise RuntimeError(f"Failed to get clip: {exc}") from exc
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Editor state — manual overrides per clip (B15/B16)
# ---------------------------------------------------------------------------

async def get_editor_state(clip_id: str) -> Optional[dict]:
    """Return the saved editor state JSON for a clip, or None if never saved."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute("SELECT state_json FROM clip_editor_state WHERE clip_id = ?", (clip_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return json.loads(row["state_json"])
    except Exception as exc:
        raise RuntimeError(f"Failed to get editor state: {exc}") from exc
    finally:
        await conn.close()


async def save_editor_state(clip_id: str, state: dict) -> None:
    """Upsert editor state JSON for a clip (serializable, versioned)."""
    # Validate clip exists
    clip = await get_clip(clip_id)
    if not clip:
        raise ValueError(f"Clip {clip_id} not found")
    # Ensure required fields
    state = dict(state)
    state["_version"] = 1
    state["_updated_at"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    payload = json.dumps(state)
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO clip_editor_state (clip_id, state_json, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(clip_id) DO UPDATE SET state_json=excluded.state_json, updated_at=CURRENT_TIMESTAMP""",
            (clip_id, payload),
        )
        await conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to save editor state: {exc}") from exc
    finally:
        await conn.close()


async def save_clip_render(clip_id: str, project_id: str, file_path: str,
                         output_hash: Optional[str], mode: str, canvas: Optional[str],
                         composition: Optional[dict], render_settings: Optional[dict]) -> str:
    """Persist one render-history record (mode/canvas/composition/output hash)."""
    rid = str(uuid.uuid4())
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO clip_renders
               (id, clip_id, project_id, file_path, output_hash, mode, canvas,
                composition_json, render_settings_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rid, clip_id, project_id, file_path, output_hash, mode, canvas,
             json.dumps(composition) if composition else None,
             json.dumps(render_settings) if render_settings else None),
        )
        await conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to save clip render: {exc}") from exc
    finally:
        await conn.close()
    return rid


async def get_clip_renders(clip_id: str) -> list[dict]:
    """Render history for a clip, newest last."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute(
            "SELECT * FROM clip_renders WHERE clip_id = ? ORDER BY created_at ASC", (clip_id,))
        rows = await cursor.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("composition_json", "render_settings_json"):
                try:
                    d[k[:-5]] = json.loads(d[k]) if d.get(k) else None
                except Exception:
                    d[k[:-5]] = None
            out.append(d)
        return out
    finally:
        await conn.close()


async def save_candidate_evaluation(project_id: str, candidate: dict) -> str:
    """Persist a candidate evaluation (including rejected). Returns id."""
    cid = str(uuid.uuid4())
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO candidate_evaluations
               (id, project_id, start_time, end_time, duration, hook_score, hook_first_3s, payoff_score, curiosity, information, narrative,
                audio_score, visual_score, reaction_score, pacing_score, final_score,
                first_3s_transcript, first_sentence, payoff_sentence, final_sentence,
                failure_reasons, hook_detail, payoff_detail, context_detail, selected)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, project_id,
             candidate.get("start"), candidate.get("end"), candidate.get("end",0)-candidate.get("start",0),
             candidate.get("hook"), candidate.get("hook_first_3s"), candidate.get("payoff"),
             candidate.get("curiosity"), candidate.get("information"), candidate.get("narrative"),
             candidate.get("audio_score"), candidate.get("visual_score"), candidate.get("reaction_score"), candidate.get("pacing_score"), candidate.get("final_score"),
             candidate.get("first_3s_transcript"), candidate.get("first_sentence"), candidate.get("payoff_sentence"), candidate.get("final_sentence"),
             json.dumps(candidate.get("failure_reasons", [])), json.dumps(candidate.get("hook_detail", {})), json.dumps(candidate.get("payoff_detail", {})), json.dumps(candidate.get("context_detail", {})),
             1 if candidate.get("selected") else 0),
        )
        await conn.commit()
        return cid
    except Exception as exc:
        raise RuntimeError(f"Failed to save candidate evaluation: {exc}") from exc
    finally:
        await conn.close()


async def get_candidate_evaluations(project_id: str) -> list[dict]:
    """Return all candidate evaluations for a project, sorted by final_score desc."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute("SELECT * FROM candidate_evaluations WHERE project_id = ? ORDER BY final_score DESC", (project_id,))
        rows = await cursor.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("failure_reasons", "hook_detail", "payoff_detail", "context_detail"):
                if d.get(k):
                    try: d[k] = json.loads(d[k])
                    except: pass
            out.append(d)
        return out
    finally:
        await conn.close()


async def save_clip_review(clip_id: str, project_id: str, decision: str, reason: Optional[str] = None, notes: Optional[str] = None) -> str:
    """Persist human review KEEP/EDIT/REJECT with reason. Returns id."""
    if decision not in ("KEEP", "EDIT", "REJECT"):
        raise ValueError("decision must be KEEP/EDIT/REJECT")
    rid = str(uuid.uuid4())
    conn = await _get_connection()
    try:
        await conn.execute(
            "INSERT INTO clip_reviews (id, clip_id, project_id, decision, reason, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, clip_id, project_id, decision, reason, notes))
        await conn.commit()
        return rid
    finally:
        await conn.close()


async def get_clip_reviews(project_id: str) -> list[dict]:
    conn = await _get_connection()
    try:
        cursor = await conn.execute("SELECT * FROM clip_reviews WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Project storage size (disk)
# ---------------------------------------------------------------------------

def _project_dir_size(project_id: str) -> int:
    """Total bytes on disk for a project's tmp directory (source + clips + renders)."""
    tmp_dir = os.getenv("TMP_DIR", os.path.join(os.path.dirname(__file__), "..", "tmp"))
    project_dir = os.path.join(tmp_dir, project_id)
    total = 0
    try:
        for root, _dirs, files in os.walk(project_dir):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# Render jobs — persisted so statuses survive backend restarts
# ---------------------------------------------------------------------------

async def upsert_render_job(job: dict) -> None:
    """Persist a render-job snapshot (single source of truth across restarts)."""
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO render_jobs
               (id, clip_id, project_id, kind, status, stage, percent, message, error,
                output, clips_status_json, updated_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
               ON CONFLICT(id) DO UPDATE SET
                 clip_id=excluded.clip_id, project_id=excluded.project_id, kind=excluded.kind,
                 status=excluded.status, stage=excluded.stage, percent=excluded.percent,
                 message=excluded.message, error=excluded.error, output=excluded.output,
                 clips_status_json=excluded.clips_status_json, updated_at=CURRENT_TIMESTAMP,
                 finished_at=excluded.finished_at""",
            (job.get("id"), job.get("clip_id"), job.get("project_id"), job.get("kind", "clip"),
             job.get("status", "queued"), job.get("stage"), float(job.get("percent") or 0),
             job.get("message"), job.get("error"), job.get("output"),
             json.dumps(job.get("clips_status")) if job.get("clips_status") else None,
             job.get("finished_at")),
        )
        await conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to persist render job: {exc}") from exc
    finally:
        await conn.close()


async def get_render_job(job_id: str) -> Optional[dict]:
    """Fetch a persisted render job by id, or None."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute("SELECT * FROM render_jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("clips_status_json"):
            try:
                d["clips_status"] = json.loads(d["clips_status_json"])
            except Exception:
                pass
        return d
    finally:
        await conn.close()


async def latest_render_job(clip_id: str, kind: Optional[str] = None) -> Optional[dict]:
    """Return the most recently created persisted render job for a clip."""
    conn = await _get_connection()
    try:
        if kind:
            cursor = await conn.execute(
                "SELECT * FROM render_jobs WHERE clip_id = ? AND kind = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (clip_id, kind))
        else:
            cursor = await conn.execute(
                "SELECT * FROM render_jobs WHERE clip_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (clip_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def latest_project_render_job(project_id: str, kind: str = "project") -> Optional[dict]:
    """Return the most recently created persisted project render job."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute(
            "SELECT * FROM render_jobs WHERE project_id = ? AND kind = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id, kind))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def get_active_render_jobs(project_id: Optional[str] = None,
                                 clip_id: Optional[str] = None,
                                 kind: Optional[str] = None) -> list[dict]:
    """Return render jobs currently marked running/queued (optionally scoped)."""
    conn = await _get_connection()
    try:
        sql = "SELECT * FROM render_jobs WHERE status IN ('running','queued')"
        params: list = []
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if clip_id:
            sql += " AND clip_id = ?"
            params.append(clip_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC"
        cursor = await conn.execute(sql, params)
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await conn.close()


async def mark_orphaned_render_jobs() -> int:
    """Mark any running/queued render jobs as errored (backend restarted)."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute(
            """UPDATE render_jobs
               SET status='error', error='Backend restarted — job did not finish',
                   message='Backend restarted — job did not finish', updated_at=CURRENT_TIMESTAMP
               WHERE status IN ('running','queued')""")
        await conn.commit()
        n = cursor.rowcount
        return n or 0
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# A/B experiment persistence
# ---------------------------------------------------------------------------

async def save_ab_experiment(project_id: str, payload: dict) -> str:
    """Persist an A/B experiment run (ranker A vs B selections + overlap)."""
    eid = str(uuid.uuid4())
    conn = await _get_connection()
    try:
        await conn.execute(
            "INSERT INTO ab_experiments (id, project_id, payload_json) VALUES (?, ?, ?)",
            (eid, project_id, json.dumps(payload)))
        await conn.commit()
        return eid
    finally:
        await conn.close()


async def get_ab_experiments(project_id: str) -> list[dict]:
    """Return all A/B experiment runs for a project, newest first."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute(
            "SELECT * FROM ab_experiments WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        out = []
        for r in await cursor.fetchall():
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except Exception:
                pass
            out.append(d)
        return out
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Candidate lookup (candidate → clip)
# ---------------------------------------------------------------------------

async def get_candidate(candidate_id: str) -> Optional[dict]:
    """Fetch a single candidate evaluation by id."""
    conn = await _get_connection()
    try:
        cursor = await conn.execute("SELECT * FROM candidate_evaluations WHERE id = ?", (candidate_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("failure_reasons", "hook_detail", "payoff_detail", "context_detail"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        return d
    finally:
        await conn.close()
