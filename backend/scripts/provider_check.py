"""provider-check: manually triggered LIVE verification harness (v1.2 §6).

Usage:
    python scripts/provider_check.py --platform tiktok

Behavior: verify configuration → verify authentication → fetch
capabilities → perform a safe READ-ONLY operation → report normalized
status. NEVER publishes. Exit code 0 always (status is in the output);
exit 2 on harness misuse.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.configcheck import validate  # noqa: E402
from automation.provider_client import AuthorizedProviderClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only live provider check (never publishes)")
    ap.add_argument("--platform", default="tiktok")
    args = ap.parse_args()

    cfg = validate()
    client = AuthorizedProviderClient(args.platform)
    result = client.check()
    report = {
        "platform": args.platform,
        "mode": cfg["mode"],
        "config_valid": cfg["valid"],
        "capabilities": client.capabilities(),
        "check": result,
        "states": {
            "supported": True,
            "configured": result["state"] in ("AUTHENTICATED", "AVAILABLE",
                                              "OPERATIONAL"),
            "authenticated": result["state"] in ("AUTHENTICATED", "AVAILABLE",
                                                 "OPERATIONAL"),
            "available": result["state"] in ("AVAILABLE", "OPERATIONAL"),
            "operational": result["state"] == "OPERATIONAL",
        },
        "published": False,
        "note": "read-only verification only; no publication attempted",
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
