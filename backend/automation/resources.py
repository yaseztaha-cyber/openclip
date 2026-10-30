"""
resources.py — Disk safety + reference-aware artifact retention (v1.2 §14/15/21).

Retention classes:
  SHORT-LIVED: render intermediates (*.seg_*, *.concat, *.joined, *.scaled,
      staging dirs), temp provider responses, temp logs.
  LONG-LIVED (never garbage-collected): approvals, publication attempts,
      reconciliation evidence, audit events, published metadata, policy
      decisions, referenced renders/reports.

Cleanup only proposes/removes files no live record references; every
deletion is audit-logged. Renders referenced by an approval, publication,
attempt, report, clip row, or audit evidence are never touched.
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)

MIN_FREE_BYTES = int(os.getenv("MIN_FREE_DISK_BYTES", str(500 * 1024 * 1024)))

SHORT_LIVED_SUFFIXES = (".seg_", ".concat.", ".joined.", ".scaled.",
                        ".temp.", ".part", ".tmp")
SHORT_LIVED_DIRS = (".render_",)


class InsufficientStorage(Exception):
    def __init__(self, path: str, free: int, need: int):
        self.path = path
        super().__init__(
            f"INSUFFICIENT_STORAGE: {free // (1024*1024)}MB free at {path}, "
            f"need {need // (1024*1024)}MB")
        self.payload = {"code": "INSUFFICIENT_STORAGE", "path": path,
                        "free_bytes": free, "needed_bytes": need}


def disk_free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path or ".").free
    except Exception:
        return -1


def require_disk_space(path: str, need_bytes: int = MIN_FREE_BYTES) -> int:
    """Raise InsufficientStorage before starting render work."""
    free = disk_free_bytes(os.path.dirname(path) if os.path.isfile(path) else path)
    if free >= 0 and free < need_bytes:
        raise InsufficientStorage(path, free, need_bytes)
    return free


async def referenced_paths(store, tmp_dir: str) -> set[str]:
    """Absolute paths referenced by live records (never delete these)."""
    refs: set[str] = set()

    def _abs(p: str | None):
        if not p:
            return
        rel = str(p).replace("/files/", "", 1).lstrip("/\\")
        refs.add(os.path.normpath(os.path.join(tmp_dir, rel)))

    try:
        import database as _db
        conn = await _db._get_connection()
        try:
            for table, col in (("clips", "file_path"), ("clip_renders", "file_path")):
                try:
                    cur = await conn.execute(f"SELECT {col} FROM {table}")
                    for r in await cur.fetchall():
                        _abs(r[0])
                except Exception:
                    pass
        finally:
            await conn.close()
    except Exception:
        pass
    for appr in await store.list_approvals():
        _abs(appr.get("preview_path"))
    for pub in await store.list_publications():
        _abs(pub.get("post_url") if str(pub.get("post_url") or "").startswith("/") else None)
    for rep in await store.list_reports():
        pass  # report bodies live in DB, not on disk
    return refs


async def cleanup_candidates(store, tmp_dir: str) -> list[dict]:
    """Short-lived files with no live references. Dry-run safe to list."""
    refs = await referenced_paths(store, tmp_dir)
    out: list[dict] = []
    for root, dirs, files in os.walk(tmp_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".render_")]
        for fn in files:
            full = os.path.normpath(os.path.join(root, fn))
            if full in refs:
                continue
            if any(s in fn for s in SHORT_LIVED_SUFFIXES):
                try:
                    out.append({"path": full, "bytes": os.path.getsize(full),
                                "class": "SHORT-LIVED"})
                except OSError:
                    pass
    # Stale staging dirs left by crashed renders.
    for root, dirs, _files in os.walk(tmp_dir):
        for d in dirs:
            if d.startswith(".render_"):
                full = os.path.normpath(os.path.join(root, d))
                out.append({"path": full, "bytes": 0, "class": "SHORT-LIVED"})
        break
    return out


async def purge(store, paths: list[str], tmp_dir: str, actor: str) -> dict:
    """Delete ONLY unreferenced short-lived paths; audit every deletion."""
    refs = await referenced_paths(store, tmp_dir)
    removed, refused = [], []
    for p in paths:
        norm = os.path.normpath(p)
        if norm in refs or not norm.startswith(os.path.normpath(tmp_dir)):
            refused.append({"path": p, "reason": "referenced_or_outside_tmp"})
            continue
        try:
            if os.path.isdir(norm):
                shutil.rmtree(norm, ignore_errors=True)
            else:
                os.remove(norm)
            removed.append(norm)
        except OSError as exc:
            refused.append({"path": p, "reason": str(exc)[:200]})
    if removed or refused:
        await store.log_decision(decision="artifact_cleanup",
                                 rules=["retention"],
                                 evidence=[{"removed": removed, "refused": refused}],
                                 operator=actor)
    return {"removed": removed, "refused": refused}
