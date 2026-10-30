"""
side_effects.py — Runtime mode guards (Phase 2).

AUTOMATION_MODE=dry_run (the default, never changed by this code) can NEVER
perform external side effects. Every external operation must call
require_live() first — UI controls alone are not sufficient.
"""

from __future__ import annotations

import os

DRY_RUN = "dry_run"
LIVE = "live"


class DryRunBlocked(Exception):
    """Raised when code attempts an external side effect while in dry-run."""

    def __init__(self, action: str):
        self.action = action
        super().__init__(f"external action '{action}' blocked: AUTOMATION_MODE=dry_run")
        self.payload = {"action": action, "mode": mode(),
                        "reason": "dry_run_never_side_effects"}


def mode() -> str:
    return os.getenv("AUTOMATION_MODE", DRY_RUN).lower()


def is_live() -> bool:
    return mode() == LIVE


def require_live(action: str) -> None:
    """Hard gate: raise unless AUTOMATION_MODE=live."""
    if not is_live():
        raise DryRunBlocked(action)


# External side-effect categories guarded by require_live().
EXTERNAL_ACTIONS = frozenset({
    "publish", "submit", "notify_email", "notify_webhook",
    "revenue_action", "provider_mutation",
})
