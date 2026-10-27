from __future__ import annotations

from dataclasses import replace

from tests.helpers import make_competition, make_event, make_reply, project_id
from xvote.eligibility import (
    apply_account_constraints,
    deduplicate_against_existing,
    evaluate_event,
    select_survivors,
)
from xvote.models import EligibilityRules, Source, SourceType, VerificationStatus
from xvote.normalization import normalize_reply


def make_reply_event(
    competition, text, *, account="1001", reply_id="5001", created_at="2026-01-10T12:00:00Z"
):
    source = competition.sources[0]
    event = normalize_reply(
        competition,
        source,
        make_reply(text=text, account=account, reply_id=reply_id, created_at=created_at),
    )
    return evaluate_event(competition, event)


def test_explicit_support_is_eligible_and_verified() -> None:
    competition = make_competition()
    competition.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    competition.refresh_ruleset()
    event = make_reply_event(competition, "I vote for Alpha")
    assert event.project_id == project_id(competition, "Alpha")
    assert event.eligibility_status == "eligible"
    assert event.verification_status == "verified"


def test_mention_without_support_statement_requires_review() -> None:
    competition = make_competition()
    competition.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    competition.refresh_ruleset()
    event = make_reply_event(competition, "Alpha is interesting")
    assert event.verification_status == VerificationStatus.PENDING_REVIEW.value
    assert event.review_status == "unreviewed"
    assert "explicit vote statement" in event.decision_reason


def test_late_activity_is_rejected() -> None:
    competition = make_competition(ends_at="2026-01-10T00:00:00Z")
    competition.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    competition.refresh_ruleset()
    event = make_reply_event(competition, "I vote for Alpha", created_at="2026-01-11T00:00:00Z")
    assert event.eligibility_status == "ineligible"
    assert event.verification_status == "rejected"
    assert "after the voting deadline" in event.decision_reason


def _with_content_status(competition, status):
    event = make_reply_event(competition, "I vote for Alpha")
    event.normalized_value["content_status"] = status
    return evaluate_event(competition, event)


def test_deleted_content_policy_excludes_or_keeps_evidence() -> None:
    competition = make_competition()
    competition.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    competition.refresh_ruleset()
    excluded = _with_content_status(competition, "deleted")
    assert excluded.eligibility_status == "ineligible"
    assert excluded.verification_status == "rejected"
    assert "deleted" in excluded.decision_reason

    kept = make_competition(eligibility=EligibilityRules(deleted_content_remains_eligible=True))
    kept.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    kept.refresh_ruleset()
    allowed = _with_content_status(kept, "deleted")
    assert allowed.eligibility_status == "eligible"
    assert any("deletion policy" in item for item in allowed.limitations)


def test_edited_content_policy_requires_review_or_counts() -> None:
    competition = make_competition()
    competition.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    competition.refresh_ruleset()
    pending = _with_content_status(competition, "edited")
    assert pending.eligibility_status == "pending_eligibility_review"
    assert pending.verification_status == "pending_review"
    assert "edited" in pending.decision_reason

    counted = make_competition(eligibility=EligibilityRules(edits_affect_eligibility=False))
    counted.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    counted.refresh_ruleset()
    allowed = _with_content_status(counted, "edited")
    assert allowed.eligibility_status == "eligible"
    assert any("edited" in item for item in allowed.limitations)


def test_ambiguous_project_mention_needs_eligibility_review() -> None:
    competition = make_competition()
    competition.sources = [
        Source(
            source_type=SourceType.X_POST.value, methods=["reply"], url="https://x.com/a/status/9"
        )
    ]
    competition.refresh_ruleset()
    event = make_reply_event(competition, "I vote for Alpha or Beta")
    assert event.eligibility_status == "pending_eligibility_review"
    assert event.verification_status == "pending_review"
    assert "Ambiguous project association" in event.decision_reason


def test_conflicting_projects_are_disputed() -> None:
    competition = make_competition()
    alpha = project_id(competition, "Alpha")
    beta = project_id(competition, "Beta")
    first = make_event(competition, "Alpha", account="42", event_id="evt_a")
    second = make_event(
        competition, "Beta", account="42", event_id="evt_b", timestamp="2026-01-11T12:00:00Z"
    )
    apply_account_constraints(competition, [first, second])
    assert first.verification_status == VerificationStatus.DISPUTED.value
    assert second.verification_status == VerificationStatus.DISPUTED.value
    assert first.project_id == alpha and second.project_id == beta


def test_conflicting_projects_can_keep_first_when_review_disabled() -> None:
    competition = make_competition(
        eligibility=EligibilityRules(conflicting_votes_require_review=False)
    )
    first = make_event(competition, "Alpha", account="42", event_id="evt_a")
    second = make_event(
        competition, "Beta", account="42", event_id="evt_b", timestamp="2026-01-11T12:00:00Z"
    )
    apply_account_constraints(competition, [first, second])
    assert first.verification_status == VerificationStatus.VERIFIED.value
    assert second.verification_status == VerificationStatus.REJECTED.value


def test_one_vote_per_account_keeps_first_observation() -> None:
    competition = make_competition()
    first = make_event(
        competition, "Alpha", account="42", event_id="evt_first", timestamp="2026-01-10T10:00:00Z"
    )
    second = make_event(
        competition, "Alpha", account="42", event_id="evt_second", timestamp="2026-01-10T11:00:00Z"
    )
    apply_account_constraints(competition, [first, second])
    assert first.verification_status == VerificationStatus.VERIFIED.value
    assert second.verification_status == VerificationStatus.REJECTED.value
    assert "one-vote-per-account" in second.decision_reason


def test_dedupe_keeps_higher_confidence_observation() -> None:
    competition = make_competition()
    low = make_event(competition, "Alpha", event_id="evt_low", confidence="low", source_id="src-a")
    high = make_event(
        competition, "Alpha", event_id="evt_high", confidence="high", source_id="src-b"
    )
    low.dedupe_key = high.dedupe_key = "same-key"
    to_store, audit, superseded = deduplicate_against_existing([high], [low])
    assert {event.event_id for event in to_store} == {"evt_high", "evt_low"}
    assert low.superseded_by == "evt_high"
    assert [event.event_id for event in superseded] == ["evt_low"]
    assert audit and audit[0].action == "supersede_observation"


def test_event_ids_are_stable_per_adapter_but_not_across_adapters() -> None:
    competition = make_competition()
    source = Source(
        source_type=SourceType.X_POST.value,
        source_id="src-shared",
        methods=["reply"],
        url="https://x.com/a/status/9",
    )
    competition.sources = [source]
    competition.refresh_ruleset()
    reply = make_reply(text="I vote for Alpha")
    first = normalize_reply(competition, source, reply)
    same = normalize_reply(competition, source, reply)
    other = normalize_reply(competition, source, replace(reply, source_tool="other-tool"))
    assert first.event_id == same.event_id
    assert first.event_id != other.event_id


def test_same_deterministic_id_is_not_marked_superseded_by_itself() -> None:
    competition = make_competition()
    low = make_event(competition, "Alpha", event_id="evt_same", confidence="low")
    high = make_event(competition, "Alpha", event_id="evt_same", confidence="high")
    survivors, pairs = select_survivors([low, high])
    assert [event.event_id for event in survivors] == ["evt_same"]
    assert survivors[0].confidence_label == "high"
    assert survivors[0].superseded_by == ""
    assert pairs == []
