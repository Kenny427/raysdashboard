#!/usr/bin/env python3
"""Refresh the dashboard blocklist review JSON.

Advisory only: reads the active Flipping Copilot profile and market/personal data,
then writes blocklist_review.json. It never auto-applies block/unblock edits.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DASHBOARD_DIR))

import server  # noqa: E402


def main() -> int:
    review = server.build_blocklist_review(save=True)
    stats = review.get("stats", {})
    suggestions = review.get("suggestions", {})
    out = {
        "ok": True,
        "profile": stats.get("profile_name"),
        "reviewed_at": stats.get("reviewed_at"),
        "allowed": stats.get("allowed_count"),
        "blocked": stats.get("blocked_count"),
        "test_unblock": len(suggestions.get("test_unblock", [])),
        "consider_block": len(suggestions.get("consider_block", [])),
        "path": str(server.BLOCKLIST_REVIEW_PATH),
        "advisory_only": True,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
