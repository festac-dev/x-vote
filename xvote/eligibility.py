"""Deterministic eligibility, verification and duplicate handling."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    AGGREGATE_METHODS,
    CONFIDENCE_ORDER,
    AuditRecord,
    Competition,
    Confidence,
    EligibilityStatus,
    NormalizedEvent,
    PollAggregate,
    ReviewStatus,
    VerificationStatus,
    VotingMethod,
    parse_timestamp,
    stable_hash,
)
from .textmatch import (
    find_hashtags,
    find_mentions,
    has_support_statement,
    match_projects,
    phrase_matches,
    suggest_intent,
)


class ConfigurationError(ValueError):
    pass


def competition_window(competition: Competition) -> tuple[datetime | None, datetime | None]:
    try:
        zone = ZoneInfo(competition.timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"unknown timezone: {competition.timezone_name}") from exc
    start = _parse_competition_time(competition.starts_at, zone)
    end = _parse_competition_time(competition.ends_at, zone)
    if start and end and end < start:
        raise ConfigurationError("competition end is before its start")
    return start, end


def _parse_competition_time(value: str, zone: ZoneInfo) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigurationError(f"invalid competition timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def _text(event: NormalizedEvent) -> str:
    return str(event.normalized_value.get("text_excerpt") or "")


def _strictness_threshold(competition: Competition) -> int:
    label = competition.evidence.minimum_confidence
    try:
        minimum = CONFIDENCE_ORDER[Confidence(label)]
    except ValueError:
        minimum = CONFIDENCE_ORDER[Confidence.LOW]
    if competition.eligibility.strictness == "strict":
        minimum = max(minimum, CONFIDENCE_ORDER[Confidence.MODERATE])
    return minimum


def _set_decision(
    event: NormalizedEvent,
    *,
    eligibility: EligibilityStatus,
    verification: VerificationStatus,
    reason: str,
    confidence: Confidence | None = None,
    review_status: ReviewStatus | None = None,
) -> None:
    event.eligibility_status = eligibility.value
    event.verification_status = verification.value
    event.decision_reason = reason
    if confidence is not None:
        event.confidence_label = confidence.value
    if review_status is not None:
        event.review_status = review_status.value


def evaluate_event(competition: Competition, event: NormalizedEvent) -> NormalizedEvent:
    """Apply the approved ruleset to one observation.

    This function is deterministic: same event + same ruleset => same result.
    It never upgrades an unresolved event to verified.
    """
    rules = competition.eligibility
    evidence = competition.evidence
    text = _text(event)
    method = event.voting_method

    manual_label = str(event.normalized_value.get("manual_project_label") or "")
    matches = match_projects(manual_label or text, competition.projects)
    event.normalized_value["matched_projects"] = [match.to_dict() for match in matches]
    source_project_id = str(event.normalized_value.get("source_project_id") or "")
    if matches:
        event.project_id = matches[0].project_id
    elif source_project_id:
        event.project_id = source_project_id
    else:
        event.project_id = ""

    if len(matches) > 1:
        candidates = ", ".join(match.project_name for match in matches)
        _set_decision(
            event,
            eligibility=EligibilityStatus.PENDING_ELIGIBILITY_REVIEW,
            verification=VerificationStatus.PENDING_REVIEW,
            reason=f"Ambiguous project association: {candidates}",
            confidence=Confidence.LOW,
            review_status=ReviewStatus.IN_REVIEW,
        )
        return event
    if not event.project_id:
        _set_decision(
            event,
            eligibility=EligibilityStatus.ELIGIBILITY_UNKNOWN,
            verification=VerificationStatus.PENDING_REVIEW,
            reason="No configured project could be associated with this activity.",
            confidence=Confidence.INSUFFICIENT_EVIDENCE,
        )
        return event

    content_status = str(event.normalized_value.get("content_status") or "unchanged").casefold()
    if content_status == "deleted":
        if not rules.deleted_content_remains_eligible:
            _set_decision(
                event,
                eligibility=EligibilityStatus.INELIGIBLE,
                verification=VerificationStatus.REJECTED,
                reason=(
                    "Source content was reported deleted; the configured policy excludes "
                    "deleted content."
                ),
                review_status=ReviewStatus.REVIEWED,
            )
            return event
        event.limitations.append(
            "Source content was reported deleted; it remains eligible under the configured "
            "deletion policy."
        )
    elif content_status == "edited":
        if rules.edits_affect_eligibility:
            _set_decision(
                event,
                eligibility=EligibilityStatus.PENDING_ELIGIBILITY_REVIEW,
                verification=VerificationStatus.PENDING_REVIEW,
                reason=(
                    "Source content was reported edited; the configured policy requires "
                    "operator re-review before counting it."
                ),
                confidence=Confidence.LOW,
                review_status=ReviewStatus.IN_REVIEW,
            )
            return event
        event.limitations.append(
            "Source content was reported edited; the configured policy keeps edited "
            "content eligible."
        )

    hard_reasons: list[str] = []
    soft_reasons: list[str] = []
    tags = set(find_hashtags(text))

    allowed_methods = set(rules.allowed_methods or competition.methods)
    if method not in allowed_methods:
        hard_reasons.append(f"Voting method '{method}' is not allowed for this competition.")
    source = competition.source_by_id(event.source_id)
    if source and source.methods and method not in source.methods:
        hard_reasons.append(f"Voting method '{method}' is not enabled on this source.")
    if rules.required_source_ids and event.source_id not in rules.required_source_ids:
        hard_reasons.append("Activity did not originate from a required source.")
    if evidence.require_source_url and not event.source_url:
        soft_reasons.append("No source URL is attached to the observation.")
    if evidence.require_timestamp:
        try:
            event_time = parse_timestamp(event.event_timestamp)
        except ValueError:
            event_time = None
        if event_time is None:
            soft_reasons.append("No reliable event timestamp is available.")

    try:
        start, end = competition_window(competition)
    except ConfigurationError:
        raise
    try:
        event_time = parse_timestamp(event.event_timestamp)
    except ValueError:
        event_time = None
    if event_time is None:
        soft_reasons.append("Event timestamp could not be normalized to UTC.")
    else:
        if start and event_time < start:
            hard_reasons.append("Activity occurred before the voting window opened.")
        if end and event_time > end:
            hard_reasons.append("Activity occurred after the voting deadline.")

    if method == VotingMethod.HASHTAG.value:
        configured = {
            tag.casefold() for project in competition.projects for tag in project.hashtags
        }
        if not configured & tags:
            soft_reasons.append("No configured project hashtag is present in the text.")
    if method == VotingMethod.KEYWORD.value:
        if not any(
            phrase_matches(text, keyword)
            for project in competition.projects
            for keyword in project.keywords
        ):
            soft_reasons.append("No configured project keyword is present in the text.")
    if method == VotingMethod.MENTION.value:
        configured_users = {
            user.casefold() for project in competition.projects for user in project.usernames
        }
        if not configured_users & set(find_mentions(text)):
            soft_reasons.append("No configured project account is mentioned in the text.")

    excluded = [
        phrase for phrase in rules.excluded_keywords if phrase.casefold() in text.casefold()
    ]
    if excluded:
        hard_reasons.append("Excluded phrase present: " + ", ".join(excluded))

    required_keywords = [
        phrase for phrase in rules.required_keywords if phrase.casefold() not in text.casefold()
    ]
    if required_keywords:
        hard_reasons.append("Required keyword missing: " + ", ".join(required_keywords))
    missing_tags = [tag for tag in rules.required_hashtags if tag.casefold() not in tags]
    if missing_tags:
        hard_reasons.append("Required hashtag missing: " + ", ".join("#" + t for t in missing_tags))

    intent = suggest_intent(text)
    explicit_support = has_support_statement(text)
    if (
        method in {VotingMethod.REPLY.value, VotingMethod.QUOTE.value}
        and rules.require_support_statement
    ):
        if not explicit_support:
            soft_reasons.append(
                "No explicit vote statement was found; a project mention alone is not a vote."
            )
    if intent.classification == "spam":
        soft_reasons.append("Text resembles spam; human review is required.")
    if intent.classification == "criticism":
        soft_reasons.append("Text contains criticism; classification is disputed.")
    if intent.classification == "question" and not explicit_support:
        soft_reasons.append("Text is question-like rather than an explicit vote.")
    if intent.classification == "question" and explicit_support:
        soft_reasons.append("Text combines a question with vote language.")
    if (
        intent.classification == "ambiguous"
        and method
        in {
            VotingMethod.REPLY.value,
            VotingMethod.QUOTE.value,
            VotingMethod.MENTION.value,
        }
        and rules.require_support_statement
    ):
        soft_reasons.append("Vote intent could not be established deterministically.")

    if not event.account_id:
        event.individual_identity_available = False
        if evidence.require_identity_for_votes:
            soft_reasons.append("No individual account identity was exposed by the source.")
        else:
            soft_reasons.append(
                "Account identity is unavailable; unique-account and "
                "human-uniqueness claims are unsupported."
            )

    if event.confidence_label not in {item.value for item in Confidence}:
        event.confidence_label = Confidence.INSUFFICIENT_EVIDENCE.value
    if CONFIDENCE_ORDER[Confidence(event.confidence_label)] < _strictness_threshold(competition):
        soft_reasons.append(
            f"Confidence '{event.confidence_label}' is below the approved verification threshold."
        )
    if CONFIDENCE_ORDER[Confidence(event.confidence_label)] < CONFIDENCE_ORDER[Confidence.LOW]:
        soft_reasons.append("Evidence is insufficient to count this observation.")

    if hard_reasons:
        _set_decision(
            event,
            eligibility=EligibilityStatus.INELIGIBLE,
            verification=VerificationStatus.REJECTED,
            reason=" ".join(dict.fromkeys(hard_reasons)),
        )
        return event

    if soft_reasons:
        disputed = intent.classification == "criticism" or (
            intent.classification == "question" and explicit_support
        )
        _set_decision(
            event,
            eligibility=EligibilityStatus.PENDING_ELIGIBILITY_REVIEW,
            verification=VerificationStatus.DISPUTED
            if disputed
            else VerificationStatus.PENDING_REVIEW,
            reason=" ".join(dict.fromkeys(soft_reasons)),
            confidence=Confidence.LOW
            if CONFIDENCE_ORDER[Confidence(event.confidence_label)]
            > CONFIDENCE_ORDER[Confidence.LOW]
            else Confidence(event.confidence_label),
            review_status=ReviewStatus.IN_REVIEW if disputed else ReviewStatus.UNREVIEWED,
        )
        return event

    _set_decision(
        event,
        eligibility=EligibilityStatus.ELIGIBLE,
        verification=VerificationStatus.VERIFIED,
        reason=(
            "Observed activity matches the approved inclusion rules; evidence reference retained."
        ),
    )
    return event


def select_survivors(
    events: list[NormalizedEvent],
) -> tuple[list[NormalizedEvent], list[tuple[NormalizedEvent, NormalizedEvent]]]:
    """Collapse technical duplicates, retaining the highest-confidence observation."""
    by_key: dict[str, list[NormalizedEvent]] = {}
    for event in events:
        if event.superseded_by:
            continue
        by_key.setdefault(event.dedupe_key or event.event_id, []).append(event)
    survivors: list[NormalizedEvent] = []
    superseded_pairs: list[tuple[NormalizedEvent, NormalizedEvent]] = []
    for key in sorted(by_key):
        group = sorted(
            by_key[key],
            key=lambda event: (
                -CONFIDENCE_ORDER.get(Confidence(event.confidence_label), 0),
                event.event_id,
            ),
        )
        winner = group[0]
        survivors.append(winner)
        for loser in group[1:]:
            if loser.event_id == winner.event_id:
                # The same deterministic ID can reappear on re-collection. Keeping one
                # row is sufficient; marking it superseded by itself would hide the winner.
                continue
            loser.superseded_by = winner.event_id
            superseded_pairs.append((winner, loser))
    return survivors, superseded_pairs


def _sort_key(event: NormalizedEvent) -> tuple[str, str]:
    return (event.event_timestamp or event.collected_at or "", event.event_id)


def _save_pre_constraint(event: NormalizedEvent) -> None:
    if "_pre_account" in event.normalized_value:
        return
    event.normalized_value["_pre_account"] = {
        "eligibility_status": event.eligibility_status,
        "verification_status": event.verification_status,
        "decision_reason": event.decision_reason,
        "confidence_label": event.confidence_label,
        "review_status": event.review_status,
    }


def _restore_pre_constraint(event: NormalizedEvent) -> None:
    pre = event.normalized_value.get("_pre_account")
    if not isinstance(pre, dict):
        return
    for key in (
        "eligibility_status",
        "verification_status",
        "decision_reason",
        "confidence_label",
        "review_status",
    ):
        if key in pre:
            setattr(event, key, pre[key])


def _apply_constraint(
    event: NormalizedEvent,
    *,
    eligibility: EligibilityStatus,
    verification: VerificationStatus,
    reason: str,
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED,
) -> None:
    _save_pre_constraint(event)
    event.eligibility_status = eligibility.value
    event.verification_status = verification.value
    event.decision_reason = reason
    event.review_status = review_status.value


def apply_account_constraints(
    competition: Competition, events: list[NormalizedEvent]
) -> list[NormalizedEvent]:
    """Enforce one-vote and conflicting-vote rules deterministically.

    Manual decisions are never overwritten. Constraint decisions are stored with
    their pre-constraint state so recalculation is idempotent.
    """
    # Restore prior constraint-only decisions before re-evaluating.
    for event in events:
        if not event.manual_decision:
            _restore_pre_constraint(event)

    rules = competition.eligibility
    candidates = [
        event
        for event in events
        if not event.superseded_by
        and not event.manual_decision
        and event.eligibility_status == EligibilityStatus.ELIGIBLE.value
        and event.verification_status
        in {
            VerificationStatus.VERIFIED.value,
            VerificationStatus.NOT_INDIVIDUALLY_VERIFIABLE.value,
        }
        and event.account_id
    ]
    if not candidates:
        return events

    def group_key(event: NormalizedEvent, include_project: bool, include_method: bool) -> tuple:
        return (
            event.account_id,
            event.project_id if include_project else "",
            event.voting_method if include_method else "",
        )

    # Per-method numeric limits.
    for method, limit in rules.per_method_limits.items():
        groups: dict[tuple, list[NormalizedEvent]] = {}
        for event in candidates:
            if event.voting_method != method:
                continue
            groups.setdefault(group_key(event, True, False), []).append(event)
        for group in groups.values():
            ordered = sorted(group, key=_sort_key)
            for event in ordered[limit:]:
                _apply_constraint(
                    event,
                    eligibility=EligibilityStatus.INELIGIBLE,
                    verification=VerificationStatus.REJECTED,
                    reason=f"Per-method limit of {limit} exceeded for this account and project.",
                )

    # One vote per account/project (optionally per method).
    if rules.one_vote_per_account:
        groups = {}
        for event in candidates:
            if event.verification_status != VerificationStatus.VERIFIED.value:
                continue
            groups.setdefault(
                group_key(event, include_project=True, include_method=rules.apply_per_method), []
            ).append(event)
        for group in groups.values():
            if len(group) <= 1:
                continue
            ordered = sorted(group, key=_sort_key)
            if rules.first_or_last == "last":
                keep = ordered[-1]
            else:
                keep = ordered[0]
            for event in ordered:
                if event is keep:
                    continue
                _apply_constraint(
                    event,
                    eligibility=EligibilityStatus.INELIGIBLE,
                    verification=VerificationStatus.REJECTED,
                    reason=(
                        "Duplicate submission under the one-vote-per-account rule; "
                        f"the {'last' if rules.first_or_last == 'last' else 'first'} "
                        "eligible observation was retained."
                    ),
                )

    # Conflicting choices for different projects.
    if rules.one_vote_per_account and not rules.allow_multiple_projects:
        groups = {}
        for event in candidates:
            if event.verification_status != VerificationStatus.VERIFIED.value:
                continue
            groups.setdefault((event.account_id,), []).append(event)
        for group in groups.values():
            projects = {event.project_id for event in group if event.project_id}
            if len(projects) <= 1:
                continue
            if rules.conflicting_votes_require_review:
                for event in group:
                    _apply_constraint(
                        event,
                        eligibility=EligibilityStatus.ELIGIBLE,
                        verification=VerificationStatus.DISPUTED,
                        reason=(
                            "The same account activity spans multiple projects; conflicting "
                            "votes require human review under the approved rules."
                        ),
                        review_status=ReviewStatus.IN_REVIEW,
                    )
            else:
                ordered = sorted(group, key=_sort_key)
                keep = ordered[-1] if rules.first_or_last == "last" else ordered[0]
                for event in group:
                    if event is keep:
                        continue
                    _apply_constraint(
                        event,
                        eligibility=EligibilityStatus.INELIGIBLE,
                        verification=VerificationStatus.REJECTED,
                        reason=(
                            "Conflicting vote for another project; "
                            "deterministic rule retained one choice."
                        ),
                    )
    return events


def deduplicate_against_existing(
    incoming: list[NormalizedEvent], existing: list[NormalizedEvent]
) -> tuple[list[NormalizedEvent], list[AuditRecord], list[NormalizedEvent]]:
    """Merge a new batch into existing observations at the dedupe-key level."""
    combined = [event for event in existing if not event.superseded_by] + incoming
    survivors, pairs = select_survivors(combined)
    audit: list[AuditRecord] = []
    superseded_events: list[NormalizedEvent] = []
    for winner, loser in pairs:
        audit.append(
            AuditRecord(
                audit_id="aud_" + stable_hash(winner.event_id, loser.event_id, length=16),
                timestamp=winner.collected_at,
                actor="duplicate-detector",
                action="supersede_observation",
                competition_id=winner.competition_id,
                event_id=loser.event_id,
                before={"source_tool": loser.source_tool, "confidence": loser.confidence_label},
                after={"source_tool": winner.source_tool, "confidence": winner.confidence_label},
                note=f"Kept higher-confidence observation {winner.event_id}.",
            )
        )
        superseded_events.append(loser)
    existing_ids = {event.event_id for event in existing}
    incoming_objects = {id(event) for event in incoming}
    to_store = [
        event
        for event in survivors
        if event.event_id not in existing_ids or id(event) in incoming_objects
    ] + superseded_events
    return to_store, audit, superseded_events


def evaluate_poll(
    competition: Competition,
    poll: PollAggregate,
    *,
    match_option: str,
) -> PollAggregate:
    rule_methods = set(competition.eligibility.allowed_methods or competition.methods)
    if VotingMethod.NATIVE_POLL.value not in rule_methods:
        poll.eligibility_status = EligibilityStatus.INELIGIBLE.value
        poll.verification_status = VerificationStatus.REJECTED.value
        poll.decision_reason = "Native poll aggregation is not enabled for this competition."
        return poll
    project = None
    target = match_option.strip().casefold()
    for candidate in competition.projects:
        values = {
            candidate.name.casefold(),
            *(alias.casefold() for alias in candidate.aliases),
            *([candidate.external_id.casefold()] if candidate.external_id else []),
        }
        if target in values:
            project = candidate
            break
    if project is None:
        matches = match_projects(match_option, competition.projects)
        if len(matches) == 1:
            project = competition.project_by_id(matches[0].project_id)
    if project is None:
        poll.project_id = ""
        poll.eligibility_status = EligibilityStatus.PENDING_ELIGIBILITY_REVIEW.value
        poll.verification_status = VerificationStatus.INCONCLUSIVE.value
        poll.decision_reason = (
            f"Poll option '{match_option}' does not map unambiguously to a configured project."
        )
        return poll
    poll.project_id = project.project_id
    if poll.manually_supplied and competition.evidence.manual_requires_review:
        poll.verification_status = VerificationStatus.PENDING_REVIEW.value
        poll.decision_reason = "Manually supplied aggregate awaiting operator review."
        poll.limitations.append(
            "Manually supplied aggregate total; review required before confirmation."
        )
        return poll
    if not poll.finished:
        poll.verification_status = VerificationStatus.PENDING_REVIEW.value
        poll.decision_reason = "Poll was observed before it finished; counts are provisional."
        return poll
    poll.verification_status = VerificationStatus.NOT_INDIVIDUALLY_VERIFIABLE.value
    poll.decision_reason = (
        "Poll finished; aggregate option counts verified against the source. Individual voter "
        "identities are not available."
    )
    return poll


def apply_manual_review(
    competition: Competition,
    event: NormalizedEvent,
    *,
    action: str,
    project_id: str = "",
    note: str = "",
    actor: str = "operator",
) -> AuditRecord:
    if not competition.eligibility.manual_overrides:
        raise ValueError("manual overrides are disabled for this competition")
    before = {
        "project_id": event.project_id,
        "eligibility_status": event.eligibility_status,
        "verification_status": event.verification_status,
        "review_status": event.review_status,
    }
    action = action.lower().strip()
    if action == "approve":
        if project_id:
            if not competition.project_by_id(project_id):
                raise ValueError("unknown project for reassignment")
            event.project_id = project_id
        elif not event.project_id:
            raise ValueError("cannot approve an event without an associated project")
        event.eligibility_status = EligibilityStatus.ELIGIBLE.value
        event.verification_status = VerificationStatus.VERIFIED.value
        event.review_status = ReviewStatus.MANUAL_OVERRIDE.value
        event.decision_reason = "Manually approved by operator." + (
            f" Note: {note}" if note else ""
        )
    elif action == "reject":
        event.eligibility_status = EligibilityStatus.INELIGIBLE.value
        event.verification_status = VerificationStatus.REJECTED.value
        event.review_status = ReviewStatus.MANUAL_OVERRIDE.value
        event.decision_reason = "Manually rejected by operator." + (
            f" Note: {note}" if note else ""
        )
    elif action == "reassign":
        if not project_id or not competition.project_by_id(project_id):
            raise ValueError("reassignment requires a known project")
        event.project_id = project_id
        event.eligibility_status = EligibilityStatus.PENDING_ELIGIBILITY_REVIEW.value
        event.verification_status = VerificationStatus.PENDING_REVIEW.value
        event.review_status = ReviewStatus.MANUAL_OVERRIDE.value
        event.decision_reason = "Project reassigned by operator; verification still required."
    elif action == "inconclusive":
        event.verification_status = VerificationStatus.INCONCLUSIVE.value
        event.review_status = ReviewStatus.MANUAL_OVERRIDE.value
        event.decision_reason = "Marked inconclusive by operator." + (
            f" Note: {note}" if note else ""
        )
    elif action == "dispute":
        event.verification_status = VerificationStatus.DISPUTED.value
        event.review_status = ReviewStatus.IN_REVIEW.value
        event.decision_reason = "Marked disputed by operator." + (f" Note: {note}" if note else "")
    else:
        raise ValueError("unsupported review action")
    event.manual_decision = True
    after = {
        "project_id": event.project_id,
        "eligibility_status": event.eligibility_status,
        "verification_status": event.verification_status,
        "review_status": event.review_status,
    }
    return AuditRecord(
        audit_id="aud_" + stable_hash(event.event_id, event.collected_at, length=16),
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        actor=actor,
        action=f"manual_{action}",
        competition_id=event.competition_id,
        event_id=event.event_id,
        before=before,
        after=after,
        note=note,
    )


def apply_manual_poll_review(
    poll: PollAggregate,
    *,
    action: str,
    note: str = "",
    actor: str = "operator",
) -> AuditRecord:
    before = {
        "verification_status": poll.verification_status,
        "decision_reason": poll.decision_reason,
    }
    poll.manual_decision = True
    if action == "approve":
        poll.verification_status = VerificationStatus.NOT_INDIVIDUALLY_VERIFIABLE.value
        poll.decision_reason = (
            "Manual aggregate approved. Individual voter identities remain unavailable."
        )
    elif action == "reject":
        poll.verification_status = VerificationStatus.REJECTED.value
        poll.decision_reason = "Manual aggregate rejected by operator."
    elif action == "inconclusive":
        poll.verification_status = VerificationStatus.INCONCLUSIVE.value
        poll.decision_reason = "Manual aggregate marked inconclusive."
    else:
        raise ValueError("unsupported aggregate review action")
    if note:
        poll.decision_reason += f" Note: {note}"
    return AuditRecord(
        audit_id="aud_" + stable_hash(poll.aggregate_id, poll.collected_at, length=16),
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        actor=actor,
        action=f"manual_aggregate_{action}",
        competition_id=poll.competition_id,
        event_id=poll.aggregate_id,
        before=before,
        after={
            "verification_status": poll.verification_status,
            "decision_reason": poll.decision_reason,
        },
        note=note,
    )


def coverage_status_for(method: str, collection_status: str) -> str:
    if method in AGGREGATE_METHODS:
        return collection_status
    return collection_status
