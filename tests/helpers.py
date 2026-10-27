"""Shared builders for deterministic unit tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from xvote.models import (
    Competition,
    EligibilityRules,
    EvidenceRules,
    NormalizedEvent,
    PollAggregate,
    Project,
    RankingRules,
    Source,
    SourceCoverage,
    VotingMethod,
)


def make_competition(
    *,
    methods: Iterable[str] = (VotingMethod.REPLY.value,),
    projects: Iterable[str] = ("Alpha", "Beta"),
    ranking: RankingRules | None = None,
    eligibility: EligibilityRules | None = None,
    evidence: EvidenceRules | None = None,
    starts_at: str = "2026-01-01T00:00:00Z",
    ends_at: str = "2026-01-31T23:59:59Z",
    sources: list[Source] | None = None,
) -> Competition:
    competition = Competition(
        name="Unit test competition",
        timezone_name="UTC",
        starts_at=starts_at,
        ends_at=ends_at,
        methods=list(methods),
        projects=[Project(name=name) for name in projects],
        sources=sources or [],
        ranking=ranking or RankingRules(),
        eligibility=eligibility or EligibilityRules(),
        evidence=evidence or EvidenceRules(),
    )
    competition.refresh_ruleset()
    return competition


def project_id(competition: Competition, name: str) -> str:
    return next(project.project_id for project in competition.projects if project.name == name)


def make_event(
    competition: Competition,
    project_name: str | None,
    *,
    method: str = VotingMethod.REPLY.value,
    account: str = "account-1",
    timestamp: str = "2026-01-10T12:00:00Z",
    eligibility: str = "eligible",
    verification: str = "verified",
    review: str = "unreviewed",
    event_id: str = "",
    source_id: str = "src-unit",
    confidence: str = "high",
    ruleset_version: str | None = None,
    superseded_by: str = "",
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id or f"evt_{account}_{method}_{timestamp}",
        competition_id=competition.competition_id,
        project_id=project_id(competition, project_name) if project_name else "",
        voting_method=method,
        source_type="x_post",
        source_id=source_id,
        source_url="https://x.com/unit/status/1",
        source_tool="unit-test",
        auth_tier="none",
        account_id=account,
        event_timestamp=timestamp,
        collected_at=timestamp,
        raw_evidence_reference="unit:evidence",
        normalized_value={"text_excerpt": "I vote for Alpha"},
        eligibility_status=eligibility,
        verification_status=verification,
        confidence_label=confidence,
        decision_reason="unit test",
        ruleset_version=ruleset_version or competition.ruleset_version,
        review_status=review,
        individual_identity_available=bool(account),
        dedupe_key=f"{method}|{account}|{timestamp}",
        superseded_by=superseded_by,
    )


def make_poll(
    competition: Competition,
    project_name: str,
    *,
    votes: int = 10,
    option_label: str | None = None,
    method: str = VotingMethod.NATIVE_POLL.value,
    verification: str = "not_individually_verifiable",
    manual: bool = False,
    aggregate_id: str = "",
) -> PollAggregate:
    return PollAggregate(
        aggregate_id=aggregate_id or f"agg_{project_name}_{method}",
        competition_id=competition.competition_id,
        project_id=project_id(competition, project_name),
        source_id="src-unit",
        source_url="https://x.com/unit/status/1",
        source_tool="unit-test",
        auth_tier="none",
        option_label=option_label or project_name,
        votes=votes,
        total_votes=max(votes * 2, votes),
        finished=True,
        collected_at="2026-01-10T12:00:00Z",
        raw_evidence_reference="unit:poll",
        confidence_label="moderate",
        verification_status=verification,
        decision_reason="unit test aggregate",
        ruleset_version=competition.ruleset_version,
        manually_supplied=manual,
        voting_method=method,
    )


def make_coverage(competition: Competition, sources: Iterable[Source]) -> dict[str, SourceCoverage]:
    return {
        source.source_id: SourceCoverage(
            source_id=source.source_id,
            source_type=source.source_type,
            label=source.label or "unit source",
            collected_methods=list(source.methods),
            individual_voters_identifiable=True,
            exact_counts_available=True,
            collection_status="complete_for_scope",
            last_checked="2026-01-10T12:00:00Z",
        )
        for source in sources
    }


def make_reply(
    *,
    text: str,
    account: str = "1001",
    username: str = "unit_user",
    reply_id: str = "5001",
    created_at: str = "2026-01-10T12:00:00Z",
    parent_post_id: str = "9001",
) -> Any:
    from xvote.adapters.base import NormalizedReply

    return NormalizedReply(
        reply_id=reply_id,
        author_id=account,
        author_username=username,
        text=text,
        created_at=created_at,
        url=f"https://x.com/{username}/status/{reply_id}",
        parent_post_id=parent_post_id,
        source_tool="unit-test",
        raw_evidence_reference=f"unit:reply:{reply_id}",
    )
