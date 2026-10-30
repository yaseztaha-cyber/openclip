"""Single authoritative application/schema version (v1.4).

APP_VERSION is exposed via /api/automation/meta, readiness, reports,
logs, and the frontend. SCHEMA_VERSION is enforced at startup: a database
reporting a NEWER schema fails loudly (never silently downgraded).
"""

APP_VERSION = "1.4.0"

# 1 = core tables, 2 = automation hub tables, 3 = attempts/transitions/
# policies/qualifications + approval binding columns.
SCHEMA_VERSION = 3
