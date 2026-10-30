"""
titles.py — Title + description generation with evidence validation.

Generation uses only: clip transcript, source context, campaign requirements.
Validation flags unsupported claims / misleading statements / forbidden terms
/ campaign-rule violations → NEEDS_REVIEW, never auto-publish.
"""

from __future__ import annotations

import re

FORBIDDEN_TERMS = [
    "guaranteed", "get rich", "100% win", "risk-free", "miracle cure",
    "click here to win", "free money",
]

REJECT_TERMS = [
    "porn", "xxx", "escort service", "online casino", "sportsbook",
]

# Internal rule/policy language that must never appear in public metadata.
INTERNAL_RULE_TEXT = [
    "needs_review", "policy_excluded", "user_affiliation_exclusion",
    "exclude_gambling", "exclude_adult", "no_background_music",
    "policy_version", "eligibility",
]

METADATA_VERSION = 2

SUPERLATIVES = ["best ever", "insane", "#1", "unbelievable", "shocking", "you won't believe"]


def _transcript_text(transcript) -> str:
    if not transcript:
        return ""
    if isinstance(transcript, str):
        return transcript
    parts = []
    for seg in transcript:
        if isinstance(seg, dict):
            parts.append(str(seg.get("text", "")))
        else:
            parts.append(str(seg))
    return " ".join(p for p in parts if p).strip()


def _top_words(text: str, n: int = 8) -> list[str]:
    stop = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "is", "it",
            "that", "this", "with", "for", "you", "your", "we", "they", "he",
            "she", "was", "were", "are", "be", "as", "at", "by", "from", "but"}
    words: dict[str, int] = {}
    for w in re.findall(r"[a-zA-Z][a-zA-Z\-']{2,}", text.lower()):
        if w not in stop:
            words[w] = words.get(w, 0) + 1
    return [w for w, _ in sorted(words.items(), key=lambda kv: -kv[1])[:n]]


def generate_title(transcript, source_title: str = "", campaign: dict | None = None,
                   max_len: int = 90) -> str:
    text = _transcript_text(transcript)
    words = _top_words(text)
    base = source_title.strip() if source_title else ""
    if words:
        hook = " ".join(w.capitalize() for w in words[:4])
        title = f"{hook}" + (f" | {base[:40]}" if base else "")
    else:
        title = base or (campaign or {}).get("name", "New clip")
    title = re.sub(r"\s+", " ", title).strip()
    return title[:max_len]


def generate_description(transcript, campaign: dict | None = None,
                         source_title: str = "", max_len: int = 400) -> str:
    text = _transcript_text(transcript)
    snippet = text[:220].strip()
    lines = []
    if snippet:
        lines.append(snippet + ("..." if len(text) > 220 else ""))
    if campaign and campaign.get("name"):
        lines.append(f"Campaign: {campaign['name']}")
    if campaign and campaign.get("source_url"):
        lines.append(str(campaign["source_url"]))
    if source_title:
        lines.append(f"Source: {source_title}")
    return "\n".join(lines)[:max_len]


def validate_metadata(title: str, description: str, transcript,
                      campaign: dict | None = None) -> dict:
    """
    Returns {"verdict": VALID|NEEDS_REVIEW|REJECTED, "issues": [...]}.
    A claim word appearing in the title but absent from transcript+source
    context is treated as unsupported. Hard policy leakage (adult/gambling
    language) REJECTS outright; uncertain claims go to NEEDS_REVIEW.
    """
    issues: list[str] = []
    evidence = (_transcript_text(transcript) + " " + str((campaign or {}).get("name", ""))).lower()
    blob = f"{title}\n{description}".lower()

    for term in FORBIDDEN_TERMS:
        if term in blob:
            issues.append({"severity": "review", "issue": f"forbidden term: '{term}'"})
    for bad in REJECT_TERMS:
        if bad in blob:
            issues.append({"severity": "reject",
                           "issue": f"campaign-rule violation in metadata: '{bad}'"})
    for leaked in INTERNAL_RULE_TEXT:
        if leaked in blob:
            issues.append({"severity": "review",
                           "issue": f"internal rule text leaked into metadata: '{leaked}'"})
    # Unsupported superlatives: present in title but not evidenced.
    for sup in SUPERLATIVES:
        if sup in title.lower() and sup not in evidence:
            issues.append({"severity": "review",
                           "issue": f"unsupported claim in title: '{sup}'"})
    # Fabricated numbers: digits in title absent from evidence.
    for num in set(re.findall(r"\b\d+(?:\.\d+)?%?\b", title)):
        if num.lower() not in evidence:
            issues.append({"severity": "review",
                           "issue": f"unverified figure in title: '{num}'"})
    # Fabricated links: URLs in metadata that match neither campaign nor source.
    allowed_hosts = set()
    for u in (str((campaign or {}).get("source_url") or ""),
              str((campaign or {}).get("campaign_url") or "")):
        m = re.search(r"https?://([^/\s]+)", u)
        if m:
            allowed_hosts.add(m.group(1).lower())
    for url in set(re.findall(r"https?://[^\s)]+", blob)):
        host = re.search(r"https?://([^/\s]+)", url)
        if host and host.group(1).lower() not in allowed_hosts:
            issues.append({"severity": "review",
                           "issue": f"unverified link in metadata: '{url}'"})

    if any(i["severity"] == "reject" for i in issues):
        return {"verdict": "REJECTED", "issues": issues,
                "metadata_version": METADATA_VERSION}
    if issues:
        return {"verdict": "NEEDS_REVIEW", "issues": issues,
                "metadata_version": METADATA_VERSION}
    return {"verdict": "VALID", "issues": [],
            "metadata_version": METADATA_VERSION}


def validate_metadata_legacy(*args, **kwargs) -> dict:
    """Back-compat shim returning the v1 {ok, issues, decision} shape."""
    out = validate_metadata(*args, **kwargs)
    flat = [i["issue"] if isinstance(i, dict) else str(i) for i in out["issues"]]
    if out["verdict"] == "VALID":
        return {"ok": True, "issues": [], "decision": "OK"}
    return {"ok": False, "issues": flat, "decision": "NEEDS_REVIEW"}
