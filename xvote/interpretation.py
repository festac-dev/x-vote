"""Deterministic plain-language interpretation into a reviewable draft config.

Interpretation never activates itself. The draft is returned with ambiguities and
assumptions, and the operator must explicitly confirm it before any collection.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    AGGREGATE_METHODS,
    CollectionSettings,
    Competition,
    EligibilityRules,
    EvidenceRules,
    Project,
    RankingRules,
    Source,
    VotingMethod,
)
from .urls import parse_x_post_reference

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

TIMEZONES = {
    "west africa time": "Africa/Lagos",
    "wat": "Africa/Lagos",
    "utc": "UTC",
    "gmt": "UTC",
    "eastern time": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "central time": "America/Chicago",
    "cst": "America/Chicago",
    "pacific time": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "central european time": "Europe/Berlin",
    "cet": "Europe/Berlin",
    "cest": "Europe/Berlin",
    "east africa time": "Africa/Nairobi",
    "eat": "Africa/Nairobi",
    "south africa standard time": "Africa/Johannesburg",
    "sast": "Africa/Johannesburg",
    "british summer time": "Europe/London",
    "bst": "Europe/London",
}

METHOD_WORDS = [
    (("native poll", "poll results", "poll", "polls"), VotingMethod.NATIVE_POLL.value),
    (("reply", "replies", "comment", "comments"), VotingMethod.REPLY.value),
    (("like", "likes"), VotingMethod.LIKE.value),
    (("repost", "reposts", "retweet", "retweets"), VotingMethod.REPOST.value),
    (("quote", "quotes", "quote-post", "quote post"), VotingMethod.QUOTE.value),
    (("mention", "mentions"), VotingMethod.MENTION.value),
    (("hashtag", "hashtags"), VotingMethod.HASHTAG.value),
    (("keyword", "keywords"), VotingMethod.KEYWORD.value),
    (("manual", "manual entry"), VotingMethod.MANUAL_TOTAL.value),
    (("external", "external aggregate"), VotingMethod.EXTERNAL_AGGREGATE.value),
    (("ocr", "screenshot", "screenshots"), VotingMethod.OCR_TOTAL.value),
]

TEMPLATES: dict[str, dict[str, Any]] = {
    "native_poll": {
        "label": "Native poll",
        "description": "Aggregate poll option counts. Individual voters are not observable.",
        "methods": [VotingMethod.NATIVE_POLL.value],
        "ranking": {"mode": "confirmed"},
    },
    "reply": {
        "label": "Reply/comment votes",
        "description": "Replies with explicit vote statements for configured projects.",
        "methods": [VotingMethod.REPLY.value],
        "ranking": {"mode": "confirmed"},
    },
    "like": {
        "label": "Like-based",
        "description": "Individually observed liker accounts on project-associated posts.",
        "methods": [VotingMethod.LIKE.value],
        "ranking": {"mode": "confirmed"},
    },
    "repost": {
        "label": "Repost-based",
        "description": "Individually observed reposter accounts on project-associated posts.",
        "methods": [VotingMethod.REPOST.value],
        "ranking": {"mode": "confirmed"},
    },
    "quote": {
        "label": "Quote-post",
        "description": "Quote posts that also contain an explicit support statement.",
        "methods": [VotingMethod.QUOTE.value],
        "ranking": {"mode": "confirmed"},
    },
    "mention": {
        "label": "Mention-based",
        "description": "Mentions of configured project accounts, classified for intent.",
        "methods": [VotingMethod.MENTION.value],
        "ranking": {"mode": "confirmed"},
    },
    "hashtag": {
        "label": "Hashtag",
        "description": "Configured hashtags with inclusion and exclusion rules.",
        "methods": [VotingMethod.HASHTAG.value],
        "ranking": {"mode": "confirmed"},
    },
    "keyword": {
        "label": "Keyword",
        "description": "Configured keywords with word-boundary matching.",
        "methods": [VotingMethod.KEYWORD.value],
        "ranking": {"mode": "confirmed"},
    },
    "combined": {
        "label": "Combined/weighted",
        "description": "Multiple methods with explicit separate or weighted scoring.",
        "methods": [
            VotingMethod.REPLY.value,
            VotingMethod.LIKE.value,
            VotingMethod.REPOST.value,
        ],
        "ranking": {"mode": "weighted", "weights": {"reply": 1.0, "like": 0.5, "repost": 1.0}},
    },
    "external": {
        "label": "External aggregate",
        "description": "Operator-supplied aggregate totals with provenance.",
        "methods": [VotingMethod.EXTERNAL_AGGREGATE.value],
        "ranking": {"mode": "confirmed"},
    },
    "manual": {
        "label": "Manual entry",
        "description": "Manually entered totals or individual records, reviewed before counting.",
        "methods": [VotingMethod.MANUAL_TOTAL.value],
        "ranking": {"mode": "confirmed"},
    },
    "csv_json": {
        "label": "CSV/JSON import",
        "description": "Import validated records with provenance and confidence labels.",
        "methods": [VotingMethod.MANUAL_TOTAL.value],
        "ranking": {"mode": "confirmed"},
    },
    "ocr": {
        "label": "Screenshot/OCR",
        "description": "OCR-derived totals remain low-confidence and require review.",
        "methods": [VotingMethod.OCR_TOTAL.value],
        "ranking": {"mode": "confirmed"},
    },
    "custom": {
        "label": "Custom rules",
        "description": "Start from an empty ruleset and configure every rule explicitly.",
        "methods": [],
        "ranking": {"mode": "confirmed"},
    },
}


def _find_urls(text: str) -> list[str]:
    urls = re.findall(r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/[^\s<>\"']+", text)
    result: list[str] = []
    for raw in urls:
        candidate = raw.rstrip(".,;:)]")
        if not candidate.startswith("http"):
            candidate = "https://" + candidate
        try:
            reference = parse_x_post_reference(candidate)
        except ValueError:
            continue
        if reference.canonical_url not in result:
            result.append(reference.canonical_url)
    return result


def _extract_projects(text: str) -> tuple[list[str], list[str]]:
    ambiguities: list[str] = []
    names: list[str] = []
    candidate = ""
    match = re.search(
        r"\b(?:votes?|vote|voting)\s+for\s+(.+?)(?:\s+from\s+|\s+in\s+|\s+using\s+|[.;]|$)",
        text,
        re.IGNORECASE,
    )
    if match:
        candidate = match.group(1)
    else:
        match = re.search(
            r"\b(?:projects?|candidates?|entries?|teams?)\s*[:\-]\s*(.+?)(?:[.;]|$)",
            text,
            re.IGNORECASE,
        )
        if match:
            candidate = match.group(1)
    if candidate:
        candidate = re.sub(r"\([^)]*\)", "", candidate)
        for part in re.split(r",\s*|\s+and\s+|\s*&\s*", candidate):
            clean = part.strip(" .;:")
            if clean and clean.casefold() not in {name.casefold() for name in names}:
                names.append(clean)
    if not names:
        ambiguities.append(
            "No project names were identified. Add projects explicitly before starting."
        )
    return names[:50], ambiguities


def _extract_methods(text: str) -> tuple[list[str], list[str]]:
    lowered = text.casefold()
    found: list[str] = []
    assumptions: list[str] = []
    for words, method in METHOD_WORDS:
        if any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered) for word in words):
            if method not in found:
                found.append(method)
    if not found:
        assumptions.append(
            "No voting method was named; replies are proposed and must be confirmed."
        )
        found = [VotingMethod.REPLY.value]
    if VotingMethod.NATIVE_POLL.value in found and VotingMethod.REPLY.value in found:
        assumptions.append(
            "Poll aggregate votes and reply votes will remain separate measures; "
            "the leaderboard will show method breakdowns."
        )
    return found, assumptions


def _parse_deadline(text: str) -> tuple[str, list[str]]:
    ambiguities: list[str] = []
    match = re.search(
        r"\b(?:ends?|closes?|deadline(?:\s+is)?|until)\s+(?:at\s+|on\s+)?([^.;]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        ambiguities.append("No voting deadline was found; set one explicitly before starting.")
        return "", ambiguities
    raw = match.group(1).strip()
    lowered = raw.casefold()
    tz_name = "UTC"
    for label, zone_name in sorted(TIMEZONES.items(), key=lambda pair: -len(pair[0])):
        if re.search(rf"(?<!\w){re.escape(label)}(?!\w)", lowered):
            tz_name = zone_name
            raw = re.sub(rf"(?<!\w){re.escape(label)}(?!\w)", "", raw, flags=re.IGNORECASE).strip()
            break
    else:
        ambiguities.append("No timezone was recognized in the deadline; UTC was assumed.")
    weekday_index: int | None = None
    for weekday_name, weekday_number in WEEKDAYS.items():
        if re.search(rf"\b{weekday_name}\b", lowered):
            weekday_index = weekday_number
            raw = re.sub(rf"\b{weekday_name}\b", " ", raw, flags=re.IGNORECASE).strip()
            break
    date_value: date | None = None
    date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if date_match:
        try:
            date_value = date(
                int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            )
            raw = raw.replace(date_match.group(0), " ")
        except ValueError:
            ambiguities.append("The deadline date could not be parsed.")
    else:
        month_match = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(\d{4}))?",
            raw,
            re.IGNORECASE,
        )
        if month_match:
            year = int(month_match.group(3) or datetime.now().year)
            try:
                date_value = datetime.strptime(
                    f"{month_match.group(1)} {month_match.group(2)} {year}", "%B %d %Y"
                ).date()
                raw = raw.replace(month_match.group(0), " ")
            except ValueError:
                ambiguities.append("The deadline date could not be parsed.")
    time_match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", raw, re.IGNORECASE
    )
    if not time_match:
        ambiguities.append("The deadline time could not be parsed.")
        return "", ambiguities
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    suffix = (time_match.group(3) or "").replace(".", "").casefold()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        ambiguities.append("The deadline time is out of range.")
        return "", ambiguities
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        ambiguities.append(f"Unknown timezone '{tz_name}'; UTC was assumed.")
        zone = ZoneInfo("UTC")
        tz_name = "UTC"
    weekday_derived = False
    if date_value is None and weekday_index is not None:
        today = datetime.now(zone).date()
        date_value = today + timedelta(days=(weekday_index - today.weekday()) % 7)
        weekday_derived = True
    if date_value is None:
        today = datetime.now(zone).date()
        candidate = datetime.combine(today, time(hour, minute), tzinfo=zone)
        if candidate <= datetime.now(zone):
            date_value = today + timedelta(days=1)
        else:
            date_value = today
        ambiguities.append(
            f"The deadline date was not stated; {date_value.isoformat()} was assumed. Confirm it."
        )
    localized = datetime.combine(date_value, time(hour, minute), tzinfo=zone)
    if weekday_derived and localized <= datetime.now(zone):
        date_value += timedelta(days=7)
        localized = datetime.combine(date_value, time(hour, minute), tzinfo=zone)
        ambiguities.append(
            f"The stated weekday had already passed; {date_value.isoformat()} was assumed. "
            "Confirm it."
        )
    return localized.isoformat(), ambiguities


def _parse_ranking(text: str) -> tuple[RankingRules, list[str]]:
    lowered = text.casefold()
    ambiguities: list[str] = []
    wants_combination = any(
        phrase in lowered
        for phrase in ("combined", "combine", "total votes", "sum of", "overall total")
    )
    if (
        "unique account" in lowered
        or "unique-account" in lowered
        or "one vote per account" in lowered
    ):
        return RankingRules(mode="unique_account"), ambiguities
    if "weighted" in lowered:
        ambiguities.append(
            "Weighted ranking was requested but no numeric weights were supplied; "
            "defaults of 1.0 are proposed."
        )
        return RankingRules(
            mode="weighted",
            weights={
                VotingMethod.REPLY.value: 1.0,
                VotingMethod.LIKE.value: 1.0,
                VotingMethod.REPOST.value: 1.0,
                VotingMethod.NATIVE_POLL.value: 1.0,
            },
        ), ambiguities
    if "confirmed" in lowered or "eligible" in lowered:
        return RankingRules(
            mode="confirmed",
            combine_confirmed_with_aggregates=wants_combination,
        ), ambiguities
    ambiguities.append("Ranking method was not explicit; confirmed eligible votes were assumed.")
    return RankingRules(
        mode="confirmed", combine_confirmed_with_aggregates=wants_combination
    ), ambiguities


def interpret_instruction(instruction: str, *, competition_name: str = "") -> dict[str, Any]:
    if not instruction or not instruction.strip():
        raise ValueError("instruction is required")
    if len(instruction) > 20_000:
        raise ValueError("instruction is too long")
    text = instruction.strip()
    urls = _find_urls(text)
    names, ambiguities = _extract_projects(text)
    methods, method_assumptions = _extract_methods(text)
    ends_at, deadline_ambiguities = _parse_deadline(text)
    ranking, ranking_ambiguities = _parse_ranking(text)
    ambiguities.extend(deadline_ambiguities + ranking_ambiguities)
    if not urls:
        ambiguities.append("No X post URL or post ID was found; add at least one source.")
    projects = [
        Project(
            name=name,
            aliases=[name] if name.lower() != name else [],
        )
        for name in names
    ]
    sources = [
        Source(
            source_type="x_post",
            post_id=parse_x_post_reference(url).post_id,
            url=url,
            label=f"X post {parse_x_post_reference(url).post_id}",
            methods=list(methods),
        )
        for url in urls
    ]
    unresolved_provisional = any(
        phrase in text.casefold()
        for phrase in ("unresolved", "provisional", "show pending", "separately")
    )
    if not unresolved_provisional:
        method_assumptions.append(
            "Unresolved submissions will be shown separately and remain provisional until reviewed."
        )
        unresolved_provisional = True
    competition = Competition(
        name=competition_name.strip() or "Draft check from instruction",
        description=text[:2000],
        timezone_name=(
            "Africa/Lagos"
            if "west africa time" in text.casefold() or re.search(r"\bwat\b", text.casefold())
            else "UTC"
        ),
        starts_at="",
        ends_at=ends_at,
        projects=projects,
        sources=sources,
        methods=methods,
        eligibility=EligibilityRules(
            allowed_methods=list(methods),
            unresolved_provisional=unresolved_provisional,
            first_or_last="first",
        ),
        evidence=EvidenceRules(),
        ranking=ranking,
        collection=CollectionSettings(),
    )
    if not projects:
        ambiguities.append("At least one project is required before collection.")
    if not methods:
        ambiguities.append("At least one voting method is required before collection.")
    aggregate_methods = {method for method in methods if method in AGGREGATE_METHODS}
    individual_methods = {method for method in methods if method not in AGGREGATE_METHODS}
    if aggregate_methods and individual_methods and not ranking.combine_confirmed_with_aggregates:
        ambiguities.append(
            "This configuration mixes source-reported aggregate methods ("
            + ", ".join(sorted(aggregate_methods))
            + ") with individually evidenced methods ("
            + ", ".join(sorted(individual_methods))
            + "). Choose how to rank: keep separate counts (default), approve a combined "
            "confirmed sum (combine_confirmed_with_aggregates), use weighted mode, unique "
            "account, or a documented custom formula."
        )
    if ranking.combine_confirmed_with_aggregates:
        ambiguities.append(
            "The combined confirmed sum mixes anonymous aggregate votes with individually "
            "evidenced events. Confirm this documented formula before collection; a weighted "
            "score is never a literal vote count."
        )
    if not ends_at:
        ambiguities.append("Voting deadline must be set before collection.")
    return {
        "instruction": text,
        "requires_confirmation": True,
        "interpretation": {
            "competition": competition.to_dict(),
            "detected_projects": names,
            "detected_methods": methods,
            "detected_sources": urls,
            "deadline": ends_at,
            "timezone": competition.timezone_name,
            "ranking_mode": ranking.mode,
        },
        "assumptions": method_assumptions
        + [
            "Project identifiers use the literal names supplied; add aliases, "
            "usernames and hashtags before starting."
            if names
            else "No project identifiers were supplied."
        ],
        "ambiguities": list(dict.fromkeys(ambiguities)),
        "activation_blocked": bool(ambiguities),
        "framing": (
            "Interpretation is a proposal only. Nothing is collected until the operator confirms "
            "the configuration."
        ),
    }
