"""Normalization of adapter observations into evidence-bearing events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .adapters.base import NormalizedReply
from .adapters.manual import ManualRecord
from .models import (
    Competition,
    Confidence,
    EligibilityStatus,
    NormalizedEvent,
    ReviewStatus,
    Source,
    VerificationStatus,
    VotingMethod,
    isoformat,
    stable_hash,
    utc_now,
)
from .textmatch import (
    find_hashtags,
    find_mentions,
    match_projects,
    phrase_matches,
    suggest_intent,
)

MAX_EXCERPT = 2000


def _event_id(dedupe_key: str, source_id: str, source_tool: str) -> str:
    return "evt_" + stable_hash(source_id, source_tool, dedupe_key, length=24)


def _base_event(
    competition: Competition,
    source: Source,
    *,
    dedupe_key: str,
    voting_method: str,
    source_type: str,
    source_url: str,
    source_tool: str,
    auth_tier: str,
    account_id: str,
    event_timestamp: str,
    collected_at: str,
    raw_evidence_reference: str,
    normalized_value: dict[str, Any],
    confidence: str,
    individual_identity_available: bool,
    is_test_data: bool,
    limitations: list[str] | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=_event_id(dedupe_key, source.source_id, source_tool),
        competition_id=competition.competition_id,
        project_id="",
        voting_method=voting_method,
        source_type=source_type,
        source_id=source.source_id,
        source_url=source_url,
        source_tool=source_tool,
        auth_tier=auth_tier,
        account_id=account_id,
        event_timestamp=event_timestamp,
        collected_at=collected_at,
        raw_evidence_reference=raw_evidence_reference,
        normalized_value=normalized_value,
        eligibility_status=EligibilityStatus.ELIGIBILITY_UNKNOWN.value,
        verification_status=VerificationStatus.PENDING_REVIEW.value,
        confidence_label=confidence,
        decision_reason="",
        ruleset_version=competition.ruleset_version,
        review_status=ReviewStatus.UNREVIEWED.value,
        individual_identity_available=individual_identity_available,
        dedupe_key=dedupe_key,
        limitations=list(limitations or []),
        is_test_data=is_test_data,
    )


def choose_method(competition: Competition, source: Source, text: str, *, is_quote: bool) -> str:
    allowed = set(source.methods or competition.methods)
    if is_quote and VotingMethod.QUOTE.value in allowed:
        return VotingMethod.QUOTE.value
    if VotingMethod.REPLY.value in allowed:
        return VotingMethod.REPLY.value
    tags = find_hashtags(text)
    if VotingMethod.HASHTAG.value in allowed and any(
        tag in {h.casefold() for p in competition.projects for h in p.hashtags} for tag in tags
    ):
        return VotingMethod.HASHTAG.value
    if VotingMethod.KEYWORD.value in allowed and any(
        phrase_matches(text, keyword)
        for project in competition.projects
        for keyword in project.keywords
    ):
        return VotingMethod.KEYWORD.value
    if VotingMethod.MENTION.value in allowed and any(
        mention in {u.casefold() for p in competition.projects for u in p.usernames}
        for mention in find_mentions(text)
    ):
        return VotingMethod.MENTION.value
    if VotingMethod.REPLY.value in allowed:
        return VotingMethod.REPLY.value
    fallback = source.methods or competition.methods
    return fallback[0] if fallback else VotingMethod.REPLY.value


def normalize_reply(
    competition: Competition,
    source: Source,
    reply: NormalizedReply,
    *,
    collected_at: str | None = None,
    is_test_data: bool = False,
) -> NormalizedEvent:
    text = (reply.text or "").strip()
    matches = match_projects(text, competition.projects)
    method = choose_method(competition, source, text, is_quote=reply.is_quote)
    intent = suggest_intent(text)
    timestamp = reply.created_at or ""
    account_id = reply.author_id or (
        f"username:{reply.author_username}" if reply.author_username else ""
    )
    dedupe_key = f"{method}|reply:{reply.parent_post_id}:{reply.reply_id}"
    confidence = Confidence.HIGH.value
    limitations: list[str] = []
    if not reply.author_id:
        confidence = Confidence.MODERATE.value
        limitations.append("Observed username but not a stable numeric account ID.")
    if not timestamp:
        confidence = Confidence.LOW.value
        limitations.append("Reply timestamp was not exposed by the source.")
    if not text:
        confidence = Confidence.INSUFFICIENT_EVIDENCE.value
    value = {
        "text_excerpt": text[:MAX_EXCERPT],
        "author_username": reply.author_username,
        "matched_projects": [match.to_dict() for match in matches],
        "intent_suggestion": intent.to_dict(),
        "is_quote": reply.is_quote,
        "quoted_post_id": reply.quoted_post_id,
        "parent_post_id": reply.parent_post_id,
        "hashtags": find_hashtags(text),
        "mentions": find_mentions(text),
    }
    return _base_event(
        competition,
        source,
        dedupe_key=dedupe_key,
        voting_method=method,
        source_type=source.source_type,
        source_url=reply.url or source.url,
        source_tool=reply.source_tool,
        auth_tier=reply.auth_tier,
        account_id=account_id,
        event_timestamp=timestamp,
        collected_at=collected_at or isoformat(utc_now()),
        raw_evidence_reference=reply.raw_evidence_reference,
        normalized_value=value,
        confidence=confidence,
        individual_identity_available=bool(reply.author_id or reply.author_username),
        is_test_data=is_test_data,
        limitations=limitations,
    )


def normalize_account_event(
    competition: Competition,
    source: Source,
    account: dict[str, Any],
    *,
    method: str,
    collected_at: str | None = None,
    is_test_data: bool = False,
) -> NormalizedEvent:
    account_id = str(account.get("account_id") or "")
    username = str(account.get("username") or "")
    project = competition.project_by_id(source.project_id) if source.project_id else None
    if project is None:
        # A single-account source may correspond to exactly one project by name.
        if len(competition.projects) == 1:
            project = competition.projects[0]
    matches = (
        [
            {
                "project_id": project.project_id,
                "project_name": project.name,
                "identifier": project.name,
                "identifier_type": "source_association",
            }
        ]
        if project
        else []
    )
    now = collected_at or isoformat(utc_now())
    dedupe_key = f"{method}|{source.post_id or source.source_id}:{account_id or username}"
    limitations = [
        "Only the account identity is exposed; this is not proof of a unique human.",
        "The source did not expose the original event timestamp; observation-time policy applies.",
    ]
    return _base_event(
        competition,
        source,
        dedupe_key=dedupe_key,
        voting_method=method,
        source_type=method,
        source_url=str(account.get("source_url") or source.url),
        source_tool=str(account.get("source_tool") or ""),
        auth_tier=str(account.get("auth_tier") or ""),
        account_id=account_id or (f"username:{username}" if username else ""),
        event_timestamp=now,
        collected_at=now,
        raw_evidence_reference=str(account.get("raw_evidence_reference") or ""),
        normalized_value={
            "text_excerpt": "",
            "author_username": username,
            "matched_projects": matches,
            "source_project_id": source.project_id,
            "timestamp_is_collection_time": True,
        },
        confidence=Confidence.MODERATE.value if (account_id and matches) else Confidence.LOW.value,
        individual_identity_available=bool(account_id or username),
        is_test_data=is_test_data,
        limitations=limitations,
    )


def normalize_manual(
    competition: Competition,
    source: Source,
    record: ManualRecord,
    *,
    is_test_data: bool = False,
) -> NormalizedEvent:
    project = _match_manual_project(competition, record.project)
    matches = (
        [
            {
                "project_id": project.project_id,
                "project_name": project.name,
                "identifier": record.project,
                "identifier_type": "manual",
            }
        ]
        if project
        else []
    )
    account_id = record.account_id or ""
    now = isoformat(utc_now())
    timestamp = record.event_timestamp or now
    dedupe_key = f"manual|{source.source_id}|{record.record_id}"
    limitations = list(record.limitations)
    if not record.source_url:
        limitations.append("Manual record has no source URL.")
    return _base_event(
        competition,
        source,
        dedupe_key=dedupe_key,
        voting_method=record.method,
        source_type=source.source_type,
        source_url=record.source_url or source.url,
        source_tool="manual-import",
        auth_tier="none",
        account_id=account_id,
        event_timestamp=timestamp,
        collected_at=now,
        raw_evidence_reference=record.evidence_reference,
        normalized_value={
            "text_excerpt": record.note or record.project,
            "manual_project_label": record.project,
            "matched_projects": matches,
            "row_number": record.row_number,
            "votes": record.votes,
            "confidence_note": record.confidence,
            "content_status": record.content_status,
        },
        confidence=record.confidence,
        individual_identity_available=bool(account_id),
        is_test_data=is_test_data,
        limitations=limitations,
    )


def _match_manual_project(competition: Competition, label: str):
    target = label.strip().casefold()
    if not target:
        return None
    for project in competition.projects:
        values = {
            project.name.casefold(),
            *(alias.casefold() for alias in project.aliases),
            *([project.external_id.casefold()] if project.external_id else []),
        }
        if target in values:
            return project
    matches = match_projects(label, competition.projects)
    return competition.project_by_id(matches[0].project_id) if len(matches) == 1 else None


def collected_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
