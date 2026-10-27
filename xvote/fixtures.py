"""Synthetic fixtures for the end-to-end test and explicitly labelled demo mode.

This data is never loaded unless the operator starts a demo session or a test
constructs it directly. Every record is marked is_test_data=True and labelled
"SYNTHETIC TEST DATA".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_LABEL = "SYNTHETIC TEST DATA — NOT LIVE X RESULTS"

DEMO_POST_ID = "900000000000000001"
DEMO_BETA_POST_ID = "900000000000000002"
DEMO_GAMMA_POST_ID = "900000000000000003"

DEMO_FIXTURE: dict[str, Any] = {
    "label": FIXTURE_LABEL,
    "posts": {
        DEMO_POST_ID: {
            "post_id": DEMO_POST_ID,
            "url": f"https://x.com/demo_campaign/status/{DEMO_POST_ID}",
            "text": "Synthetic campaign post. Vote for Alpha, Beta, or Gamma. TEST DATA.",
            "created_at": "2026-10-20T09:00:00+00:00",
            "author_id": "8000001",
            "author_username": "demo_campaign",
            "metrics": {"likes": 4, "reposts": 3, "replies": 8, "quotes": 2},
        },
        DEMO_BETA_POST_ID: {
            "post_id": DEMO_BETA_POST_ID,
            "url": f"https://x.com/demo_beta/status/{DEMO_BETA_POST_ID}",
            "text": "Synthetic Beta project post. TEST DATA.",
            "created_at": "2026-10-20T09:05:00+00:00",
            "author_id": "8000002",
            "author_username": "demo_beta",
            "metrics": {"likes": 2, "reposts": 1, "replies": 0, "quotes": 0},
        },
        DEMO_GAMMA_POST_ID: {
            "post_id": DEMO_GAMMA_POST_ID,
            "url": f"https://x.com/demo_gamma/status/{DEMO_GAMMA_POST_ID}",
            "text": "Synthetic Gamma project post. TEST DATA.",
            "created_at": "2026-10-20T09:10:00+00:00",
            "author_id": "8000003",
            "author_username": "demo_gamma",
            "metrics": {"likes": 1, "reposts": 1, "replies": 0, "quotes": 0},
        },
    },
    "polls": {
        DEMO_POST_ID: {
            "post_id": DEMO_POST_ID,
            "url": f"https://x.com/demo_campaign/status/{DEMO_POST_ID}",
            "finished": True,
            "options": [
                {"label": "Alpha", "votes": 120},
                {"label": "Beta", "votes": 90},
                {"label": "Gamma", "votes": 45},
            ],
            "exact_counts": True,
            "limitations": [
                "Synthetic poll aggregate for testing only.",
                "Native poll votes are anonymous aggregates; individual voters are not observable.",
            ],
        }
    },
    "replies": {
        DEMO_POST_ID: [
            {
                "reply_id": "910000000000000001",
                "author_id": "8000101",
                "author_username": "demo_voter_one",
                "text": "I vote for Alpha. #alpha",
                "created_at": "2026-10-20T10:00:00+00:00",
                "url": "https://x.com/demo_voter_one/status/910000000000000001",
            },
            {
                "reply_id": "910000000000000002",
                "author_id": "8000102",
                "author_username": "demo_voter_two",
                "text": "Vote for Beta!",
                "created_at": "2026-10-20T10:05:00+00:00",
                "url": "https://x.com/demo_voter_two/status/910000000000000002",
            },
            {
                "reply_id": "910000000000000003",
                "author_id": "8000103",
                "author_username": "demo_voter_three",
                "text": "Vote for Beta and Gamma, both are great.",
                "created_at": "2026-10-20T10:10:00+00:00",
                "url": "https://x.com/demo_voter_three/status/910000000000000003",
            },
            {
                "reply_id": "910000000000000004",
                "author_id": "8000101",
                "author_username": "demo_voter_one",
                "text": "Also voting for Gamma now.",
                "created_at": "2026-10-20T10:15:00+00:00",
                "url": "https://x.com/demo_voter_one/status/910000000000000004",
            },
            {
                "reply_id": "910000000000000005",
                "author_id": "8000104",
                "author_username": "demo_late_voter",
                "text": "Vote for Gamma",
                "created_at": "2036-01-01T00:30:00+01:00",
                "url": "https://x.com/demo_late_voter/status/910000000000000005",
            },
            {
                "reply_id": "910000000000000006",
                "author_id": "8000105",
                "author_username": "demo_question",
                "text": "Who should I vote for?",
                "created_at": "2026-10-20T10:20:00+00:00",
                "url": "https://x.com/demo_question/status/910000000000000006",
            },
            {
                "reply_id": "910000000000000007",
                "author_id": "8000106",
                "author_username": "demo_duplicate_source",
                "text": "I vote for Alpha. #alpha",
                "created_at": "2026-10-20T10:25:00+00:00",
                "url": "https://x.com/demo_duplicate_source/status/910000000000000007",
            },
            {
                "reply_id": "910000000000000007",
                "author_username": "demo_duplicate_source",
                "text": "I vote for Alpha. #alpha",
                "source_tool": "synthetic-fixture-replay",
                "url": "https://x.com/demo_duplicate_source/status/910000000000000007",
            },
        ]
    },
    "likers": {
        DEMO_POST_ID: [
            {"account_id": "8000201", "username": "demo_liker_one"},
            {"account_id": "8000203", "username": "demo_liker_three"},
        ],
        DEMO_BETA_POST_ID: [
            {"account_id": "8000301", "username": "demo_beta_liker_one"},
            {"account_id": "8000302", "username": "demo_beta_liker_two"},
        ],
        DEMO_GAMMA_POST_ID: [
            {"account_id": "8000304", "username": "demo_gamma_liker_one"},
        ],
    },
    "reposters": {
        DEMO_POST_ID: [
            {"account_id": "8000204", "username": "demo_reposter"},
        ],
        DEMO_BETA_POST_ID: [
            {"account_id": "8000303", "username": "demo_beta_reposter"},
        ],
        DEMO_GAMMA_POST_ID: [
            {"account_id": "8000305", "username": "demo_gamma_reposter"},
        ],
    },
    "manual_totals": [
        {
            "project": "Gamma",
            "votes": 15,
            "method": "manual_total",
            "source_url": "https://example.test/manual-gamma",
            "confidence": "moderate",
            "note": "Synthetic manual aggregate for testing.",
        }
    ],
}


def load_fixture(path: str | Path) -> dict[str, Any]:
    """Load a fixture from an explicit path. Used by tests only."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not payload.get("label"):
        payload["label"] = FIXTURE_LABEL
    return payload
