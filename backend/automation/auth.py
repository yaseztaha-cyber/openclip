"""
auth.py — Server-side operator authorization (v1.4).

Local-first default: when OPERATOR_TOKEN is unset, mutating endpoints
remain open (existing behavior preserved) and the API reports
auth mode "open-local". When OPERATOR_TOKEN is set, every mutating
automation endpoint requires header X-Operator-Token (constant-time
compare); frontend-only checks never substitute for this.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException  # type: ignore


def auth_mode() -> str:
    return "operator-token" if os.getenv("OPERATOR_TOKEN", "").strip() else "open-local"


async def require_operator(x_operator_token: str | None = Header(default=None)) -> str:
    expected = os.getenv("OPERATOR_TOKEN", "").strip()
    if not expected:
        return "open-local"
    if x_operator_token and hmac.compare_digest(x_operator_token, expected):
        return "operator"
    raise HTTPException(status_code=401, detail={
        "error": {"code": "unauthorized",
                  "message": "valid X-Operator-Token required",
                  "details": {}}})
