"""
email_sink.py — Local email sink for testing (v1.6 §17).

Because SMTP is unavailable in most environments, tests and local
rehearsals can persist generated mail here instead. The sink NEVER
contacts SMTP, is always labeled LOCAL_TEST_SINK, and is never reported
as SENT. Production code paths are untouched: reports.send_email() is
only invoked with allow_external=True by an explicit operator request.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid

SINK_LABEL = "LOCAL_TEST_SINK"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sink_dir() -> str:
    root = os.getenv("EMAIL_SINK_DIR", "")
    if not root:
        raise RuntimeError("EMAIL_SINK_DIR is not configured; refusing to guess a location")
    os.makedirs(root, exist_ok=True)
    return root


def store_message(*, subject: str, html: str, text: str,
                  recipients: list[str], report_hash: str = "",
                  report_id: str = "") -> dict:
    """Persist one message to the local sink. Returns its manifest."""
    body = {"sink": SINK_LABEL, "subject": subject, "html": html,
            "text": text, "recipients": recipients,
            "report_hash": report_hash, "report_id": report_id,
            "stored_at": _now()}
    digest = hashlib.sha256(json.dumps(
        body, sort_keys=True, default=str).encode()).hexdigest()[:24]
    manifest = {**body, "sink_id": f"sink_{digest[:12]}",
                "delivery_state": "LOCAL_TEST_SINK"}
    path = os.path.join(sink_dir(), f"{manifest['sink_id']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return {"sink_id": manifest["sink_id"], "path": path,
            "delivery_state": "LOCAL_TEST_SINK",
            "report_hash": report_hash}


def list_messages() -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(sink_dir()))
    except Exception:
        return out
    for fn in names:
        if not fn.startswith("sink_") or not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(sink_dir(), fn), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except Exception:
            pass
    return out


def new_sink_id() -> str:
    return f"sink_{uuid.uuid4().hex[:12]}"
