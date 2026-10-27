"""Deterministic text and identifier matching.

All matching is case-folded and Unicode-normalized. Multi-character phrases use
word boundaries, so "Beta" does not match "Betamax" and "#Alpha" is handled as a
hashtag rather than a substring.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Project


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(phrase.strip())
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.UNICODE)


def phrase_matches(text: str, phrase: str) -> bool:
    cleaned = normalize_text(phrase).strip()
    if not cleaned:
        return False
    return bool(_phrase_pattern(cleaned).search(normalize_text(text)))


def find_hashtags(text: str) -> list[str]:
    return [
        match.group(1).casefold()
        for match in re.finditer(r"(?<![\w#])#([\w]{1,100})", text or "", re.UNICODE)
    ]


def find_mentions(text: str) -> list[str]:
    return [
        match.group(1).casefold()
        for match in re.finditer(
            r"(?<![\w@])@([A-Za-z0-9_]{1,15})(?![\w@])", text or "", re.UNICODE
        )
    ]


def find_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>\"']+", text or "", re.IGNORECASE)


@dataclass(frozen=True)
class ProjectMatch:
    project_id: str
    project_name: str
    identifier: str
    identifier_type: str

    def to_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "identifier": self.identifier,
            "identifier_type": self.identifier_type,
        }


def match_projects(
    text: str,
    projects: Iterable[Project],
    *,
    method: str = "",
    include_usernames: bool = True,
    include_hashtags: bool = True,
    include_keywords: bool = True,
    include_domains: bool = True,
) -> list[ProjectMatch]:
    """Return every project with at least one identifier observed in the text.

    Matching multiple projects is not resolved here; the eligibility engine keeps
    such events ambiguous unless deterministic rules or a human resolve them.
    """
    normalized = normalize_text(text)
    tags = set(find_hashtags(text))
    mentions = set(find_mentions(text))
    urls = " ".join(find_urls(text)).casefold()
    matches: dict[str, ProjectMatch] = {}

    for project in projects:
        identifiers: list[tuple[str, str, bool]] = [("name", project.name, True)]
        identifiers.extend(("alias", alias, True) for alias in project.aliases)
        if include_keywords:
            identifiers.extend(("keyword", keyword, True) for keyword in project.keywords)
        if include_usernames:
            identifiers.extend(("username", user, False) for user in project.usernames)
        if include_hashtags:
            identifiers.extend(("hashtag", tag, False) for tag in project.hashtags)
        if include_domains:
            identifiers.extend(("domain", domain, False) for domain in project.domains)

        for kind, identifier, _is_phrase in identifiers:
            if not identifier:
                continue
            if kind == "username":
                found = identifier.casefold() in mentions
            elif kind == "hashtag":
                found = identifier.casefold() in tags
            elif kind == "domain":
                found = bool(
                    re.search(rf"(?<![\w.-]){re.escape(identifier.casefold())}(?![\w.-])", urls)
                )
            else:
                found = phrase_matches(normalized, identifier)
            if found:
                matches.setdefault(
                    project.project_id,
                    ProjectMatch(project.project_id, project.name, identifier, kind),
                )
                break
    return list(matches.values())


def contains_any(text: str, phrases: Iterable[str]) -> list[str]:
    normalized = normalize_text(text)
    return [phrase for phrase in phrases if phrase_matches(normalized, phrase)]


SUPPORT_PHRASES = (
    "vote for",
    "voting for",
    "i vote",
    "my vote",
    "i'm voting",
    "im voting",
    "+1",
    "support",
)
CRITICISM_PHRASES = (
    "don't vote",
    "do not vote",
    "never vote",
    "worst",
    "scam",
    "fraud",
    "disqualify",
    "boycott",
)
QUESTION_MARKERS = ("?", "who should", "why", "how do", "is it", "can someone")
SPAM_MARKERS = ("dm me", "check my bio", "follow back", "free followers", "click here")

_SUPPORT_PATTERNS = (
    re.compile(r"\bi\s+vote\b", re.UNICODE),
    re.compile(r"\b(?:i\s+am|i'm|im)\s+voting\b", re.UNICODE),
    re.compile(r"\bvot(?:e|ing)\s+(?:for\s+)?[@#]?\w", re.UNICODE),
    re.compile(r"\bmy\s+vote\s+(?:goes\s+to|is\s+for)?\b", re.UNICODE),
    re.compile(r"\+1\b", re.UNICODE),
)


def has_support_statement(text: str) -> bool:
    """Detect an explicit vote statement, not merely a project mention."""
    normalized = normalize_text(text)
    return any(pattern.search(normalized) for pattern in _SUPPORT_PATTERNS)


@dataclass(frozen=True)
class IntentSuggestion:
    classification: str
    confidence: str
    reasons: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
        }


def suggest_intent(text: str) -> IntentSuggestion:
    """Heuristic review aid only. Never used to silently count or reject votes."""
    normalized = normalize_text(text)
    reasons: list[str] = []
    if any(marker in normalized for marker in SPAM_MARKERS):
        reasons.append("spam phrase")
        return IntentSuggestion("spam", "low", reasons)
    criticism = [phrase for phrase in CRITICISM_PHRASES if phrase_matches(normalized, phrase)]
    support = [phrase for phrase in SUPPORT_PHRASES if phrase_matches(normalized, phrase)]
    question = any(marker in normalized for marker in QUESTION_MARKERS)
    if criticism:
        reasons.append("critical phrase: " + ", ".join(criticism))
        return IntentSuggestion("criticism", "moderate", reasons)
    if question and not support:
        reasons.append("question-like text")
        return IntentSuggestion("question", "low", reasons)
    if support:
        reasons.append("support phrase: " + ", ".join(support))
        return IntentSuggestion("vote", "moderate", reasons)
    reasons.append("no explicit support or criticism marker")
    return IntentSuggestion("ambiguous", "insufficient_evidence", reasons)
