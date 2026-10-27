"""Built-in configuration templates.

Templates only supply draft defaults. They never collect data, alter an active
competition or bypass the confirmation/review workflow.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "native_poll",
        "name": "Native poll",
        "description": "Anonymous X poll aggregate; individual voters are not available.",
        "source_type": "poll",
        "methods": ["native_poll"],
        "eligibility": {
            "one_vote_per_account": False,
            "require_support_statement": False,
            "unresolved_provisional": True,
        },
        "ranking": {"mode": "separate", "combine_confirmed_with_aggregates": False},
        "warnings": [
            "Poll totals are source-reported aggregates and cannot be individually verified.",
        ],
    },
    {
        "id": "reply_comment",
        "name": "Reply / comment",
        "description": "Count replies that explicitly support a project.",
        "source_type": "x_post",
        "methods": ["reply"],
        "eligibility": {
            "one_vote_per_account": True,
            "require_support_statement": True,
            "conflicting_votes_require_review": True,
        },
        "ranking": {"mode": "confirmed"},
        "warnings": [
            "A project name alone is not a vote; support-statement rules and review apply.",
        ],
    },
    {
        "id": "like_based",
        "name": "Like-based",
        "description": "Count individually observed liker accounts under a configured post.",
        "source_type": "x_post",
        "methods": ["like"],
        "eligibility": {
            "one_vote_per_account": True,
            "require_support_statement": False,
            "allow_multiple_projects": False,
        },
        "ranking": {"mode": "confirmed"},
        "warnings": ["Observed accounts are not verified unique humans."],
    },
    {
        "id": "repost_based",
        "name": "Repost-based",
        "description": "Count individually observed reposter accounts.",
        "source_type": "x_post",
        "methods": ["repost"],
        "eligibility": {
            "one_vote_per_account": True,
            "require_support_statement": False,
            "allow_multiple_projects": False,
        },
        "ranking": {"mode": "confirmed"},
        "warnings": ["Reposts are not merged with quote posts unless the rules allow both."],
    },
    {
        "id": "quote_post",
        "name": "Quote-post",
        "description": "Count quote posts that contain an explicit support statement.",
        "source_type": "x_post",
        "methods": ["quote"],
        "eligibility": {"require_support_statement": True},
        "ranking": {"mode": "confirmed"},
        "warnings": ["Quoting the source post alone does not qualify."],
    },
    {
        "id": "mention_based",
        "name": "Mention-based",
        "description": "Classify mentions and count only explicit support.",
        "source_type": "mentions",
        "methods": ["mention"],
        "eligibility": {"require_support_statement": True},
        "ranking": {"mode": "confirmed"},
        "warnings": [
            "Neutral mentions, criticism, questions and spam remain unresolved or rejected."
        ],
    },
    {
        "id": "hashtag",
        "name": "Hashtag",
        "description": "Count posts carrying an approved campaign hashtag and support statement.",
        "source_type": "hashtags",
        "methods": ["hashtag"],
        "eligibility": {"require_support_statement": True},
        "ranking": {"mode": "confirmed"},
        "warnings": ["Hashtag discovery depends on the configured source and may be incomplete."],
    },
    {
        "id": "keyword",
        "name": "Keyword",
        "description": "Count posts containing configured project keywords and support language.",
        "source_type": "keywords",
        "methods": ["keyword"],
        "eligibility": {"require_support_statement": True},
        "ranking": {"mode": "confirmed"},
        "warnings": ["Keyword matches can be noisy and need review."],
    },
    {
        "id": "combined_weighted",
        "name": "Combined / weighted",
        "description": "Keep method counts visible and rank by an explicit weighted score.",
        "source_type": "x_post",
        "methods": ["reply", "like", "repost"],
        "eligibility": {"one_vote_per_account": True, "allow_multiple_projects": False},
        "ranking": {
            "mode": "weighted",
            "weights": {"reply": 1.0, "like": 1.0, "repost": 1.0},
            "combine_confirmed_with_aggregates": False,
        },
        "warnings": [
            "Weights are an operator choice; the score is not a number of votes.",
            "Incompatible measures are never summed silently.",
        ],
    },
    {
        "id": "external_aggregate",
        "name": "External aggregate",
        "description": "Import a source-reported total with provenance and confidence.",
        "source_type": "external",
        "methods": ["external_aggregate"],
        "eligibility": {"one_vote_per_account": False, "require_support_statement": False},
        "ranking": {"mode": "separate"},
        "warnings": ["External totals stay aggregate; individual identity is unavailable."],
    },
    {
        "id": "manual_entry",
        "name": "Manual entry / CSV / JSON",
        "description": "Import operator-supplied individual rows or aggregate totals.",
        "source_type": "manual",
        "methods": ["manual_total"],
        "eligibility": {"manual_overrides": True, "unresolved_provisional": True},
        "ranking": {"mode": "separate"},
        "warnings": ["Manual evidence requires provenance and review by default."],
    },
    {
        "id": "screenshot_ocr",
        "name": "Screenshot / OCR",
        "description": "Extract aggregate totals from operator-supplied images.",
        "source_type": "ocr",
        "methods": ["ocr_total"],
        "eligibility": {"one_vote_per_account": False, "require_support_statement": False},
        "ranking": {"mode": "separate"},
        "warnings": ["OCR text is evidence, not a verified ballot; image OCR is optional."],
    },
    {
        "id": "custom_rules",
        "name": "Custom rules",
        "description": "Start from reply defaults and edit every rule before collection.",
        "source_type": "x_post",
        "methods": ["reply"],
        "ranking": {"mode": "custom"},
        "warnings": [
            "Custom formulas use the safe arithmetic evaluator and are shown in every view."
        ],
    },
]


def list_templates() -> dict[str, Any]:
    return {
        "templates": deepcopy(TEMPLATES),
        "note": (
            "Templates supply defaults only. Review projects, sources, rules and ranking "
            "before creating a competition; nothing is activated or collected automatically."
        ),
    }
