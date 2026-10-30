"""
analytics.py — Metrics collection over real data only.

Unavailable metrics are reported as NOT AVAILABLE, never fabricated.
"""

from __future__ import annotations

NA = "NOT AVAILABLE"


def _num(v):
    return v if isinstance(v, (int, float)) else None


def aggregate_publications(publications: list[dict], snapshots: list[dict]) -> dict:
    """Aggregate latest snapshot per publication; missing data stays NA."""
    latest: dict[str, dict] = {}
    for s in snapshots:
        pid = s.get("publication_id") or s.get("clip_id")
        if not pid:
            continue
        if pid not in latest or str(s.get("ts", "")) > str(latest[pid].get("ts", "")):
            latest[pid] = s

    totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    earnings_confirmed, earnings_pending = 0.0, 0.0
    has_views = has_earn_c = has_earn_p = False
    top = None
    for pub in publications:
        snap = latest.get(pub.get("id")) or latest.get(pub.get("clip_id")) or {}
        v = _num(snap.get("views"))
        if v is not None:
            totals["views"] += v
            has_views = True
            if top is None or v > (top.get("views") or 0):
                top = {"clip_id": pub.get("clip_id"), "views": v,
                       "post_url": pub.get("post_url"),
                       "campaign_id": pub.get("campaign_id")}
        for k in ("likes", "comments", "shares"):
            nv = _num(snap.get(k))
            if nv is not None:
                totals[k] += nv
        ec, ep = _num(snap.get("earnings_confirmed")), _num(snap.get("earnings_pending"))
        if ec is not None:
            earnings_confirmed += ec
            has_earn_c = True
        if ep is not None:
            earnings_pending += ep
            has_earn_p = True

    engagement = totals["likes"] + totals["comments"] + totals["shares"]
    return {
        "published_count": len(publications),
        "total_views": totals["views"] if has_views else NA,
        "likes": totals["likes"],
        "comments": totals["comments"],
        "shares": totals["shares"],
        "engagement": engagement,
        "confirmed_earnings": round(earnings_confirmed, 2) if has_earn_c else NA,
        "pending_earnings": round(earnings_pending, 2) if has_earn_p else NA,
        "estimated_earnings": NA,
        "top_clip": top,
    }
