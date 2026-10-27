from __future__ import annotations

import pytest

from tests.helpers import (
    make_competition,
    make_coverage,
    make_event,
    make_poll,
)
from xvote.models import RankingRules, Source, SourceType
from xvote.ranking import (
    FormulaError,
    build_leaderboard,
    build_timeline,
    compare_snapshots,
    create_snapshot,
    safe_formula,
    validate_formula,
)


def test_mixed_methods_keep_aggregates_separate_by_default() -> None:
    competition = make_competition(methods=("native_poll", "reply"))
    events = [
        make_event(competition, "Alpha", account="a1"),
        make_event(competition, "Alpha", account="a2", timestamp="2026-01-10T13:00:00Z"),
    ]
    polls = [make_poll(competition, "Alpha", votes=100)]
    board = build_leaderboard(competition, events, polls, {})
    alpha = next(entry for entry in board.entries if entry.name == "Alpha")
    assert alpha.score == 2.0
    assert alpha.confirmed_individual == 2
    assert alpha.confirmed_aggregate == 100
    assert board.aggregates_ranked_separately is True
    assert board.confirmed_individual_total == 2
    assert board.confirmed_aggregate_total == 100
    assert "aggregates reported separately" in board.score_label
    assert any("mixes source-reported aggregate votes" in item for item in board.limitations)


def test_combined_flag_counts_aggregates_in_score() -> None:
    competition = make_competition(
        methods=("native_poll", "reply"),
        ranking=RankingRules(combine_confirmed_with_aggregates=True),
    )
    events = [make_event(competition, "Alpha", account="a1")]
    polls = [make_poll(competition, "Alpha", votes=100)]
    board = build_leaderboard(competition, events, polls, {})
    alpha = next(entry for entry in board.entries if entry.name == "Alpha")
    assert alpha.score == 101.0
    assert board.aggregates_ranked_separately is False


def test_weighted_view_never_labels_score_as_votes() -> None:
    competition = make_competition(
        methods=("reply", "like"),
        ranking=RankingRules(mode="weighted", weights={"reply": 2.0, "like": 1.0}),
    )
    events = [
        make_event(competition, "Alpha", account="a1"),
        make_event(
            competition, "Alpha", account="a2", method="like", timestamp="2026-01-10T13:00:00Z"
        ),
    ]
    board = build_leaderboard(competition, events, [], {}, view="weighted")
    alpha = next(entry for entry in board.entries if entry.name == "Alpha")
    assert alpha.score == 3.0
    assert "not a vote count" in board.score_label
    assert alpha.weighted_inputs["reply"] == 1.0
    assert "reply=2" in board.formula


def test_account_view_denominator_is_observed_accounts() -> None:
    competition = make_competition(methods=("reply", "like"))
    events = [
        make_event(competition, "Alpha", account="a1"),
        make_event(
            competition, "Alpha", account="a2", method="like", timestamp="2026-01-10T13:00:00Z"
        ),
        make_event(competition, "Beta", account="a1", timestamp="2026-01-10T14:00:00Z"),
    ]
    board = build_leaderboard(competition, events, [], {}, view="account")
    assert board.metric == "observed_accounts"
    assert board.denominator_kind.startswith("observed account IDs")
    assert sum(entry.score for entry in board.entries) == 3


def test_unresolved_events_are_counted_but_not_scored() -> None:
    competition = make_competition()
    events = [
        make_event(competition, "Alpha", account="a1"),
        make_event(
            competition,
            "Alpha",
            account="a2",
            verification="pending_review",
            review="in_review",
            timestamp="2026-01-10T13:00:00Z",
        ),
    ]
    board = build_leaderboard(competition, events, [], {})
    alpha = next(entry for entry in board.entries if entry.name == "Alpha")
    assert alpha.score == 1.0
    assert board.total_unresolved == 1
    assert alpha.unresolved_count == 1


def test_stale_ruleset_events_are_excluded() -> None:
    competition = make_competition()
    fresh = make_event(competition, "Alpha", account="a1")
    stale = make_event(
        competition, "Alpha", account="a2", timestamp="2026-01-10T13:00:00Z", ruleset_version="old"
    )
    board = build_leaderboard(competition, [fresh, stale], [], {})
    alpha = next(entry for entry in board.entries if entry.name == "Alpha")
    assert alpha.score == 1.0
    assert any("different ruleset" in item for item in board.limitations)


def test_unassigned_pending_events_are_reported_globally() -> None:
    competition = make_competition()
    events = [
        make_event(
            competition,
            None,
            account="a2",
            eligibility="eligibility_unknown",
            verification="pending_review",
            review="in_review",
            timestamp="2026-01-10T13:00:00Z",
        )
    ]
    board = build_leaderboard(competition, events, [], {})
    assert board.unassigned_events == 1
    assert board.unassigned_pending_review == 1
    assert board.total_unresolved == 1
    assert any("could not be associated with a project" in item for item in board.limitations)


def test_zero_vote_projects_are_listed() -> None:
    competition = make_competition(projects=("Alpha", "Beta"))
    board = build_leaderboard(competition, [], [], {})
    assert [entry.name for entry in board.entries] == ["Alpha", "Beta"]
    assert all(entry.score == 0 for entry in board.entries)
    assert all(entry.vote_share is None for entry in board.entries)


def test_safe_formula_accepts_basic_arithmetic_and_rejects_code() -> None:
    assert safe_formula("reply * 2 + like", {"reply": 3, "like": 4}) == 10
    assert validate_formula("reply + poll", ["reply"]) is None
    with pytest.raises(FormulaError):
        safe_formula("__import__('os').system('echo hi')", {"reply": 1})
    with pytest.raises(FormulaError):
        safe_formula("unknown + 1", {"reply": 1})
    with pytest.raises(FormulaError):
        safe_formula("reply +", {"reply": 1})


def test_snapshots_are_unique_and_comparable() -> None:
    competition = make_competition()
    source = Source(source_type=SourceType.X_POST.value, methods=["reply"])
    competition.sources = [source]
    competition.refresh_ruleset()
    coverage = make_coverage(competition, competition.sources)
    board = build_leaderboard(competition, [make_event(competition, "Alpha")], [], coverage)
    first = create_snapshot(competition, board, collection_status=board.coverage_status)
    second = create_snapshot(competition, board, collection_status=board.coverage_status)
    assert first.snapshot_id != second.snapshot_id
    diff = compare_snapshots(first, second)
    assert diff["count_changes"] == {}
    assert diff["rank_changes"] == {}


def test_timeline_reports_collection_gaps() -> None:
    competition = make_competition(methods=("reply",))
    events = [
        make_event(competition, "Alpha", account="a1", timestamp="2026-01-10T00:00:00Z"),
        make_event(competition, "Alpha", account="a2", timestamp="2026-01-10T12:01:00Z"),
    ]
    timeline = build_timeline(competition, events, [], {})
    assert any(item["type"] == "collection_gap" for item in timeline["events"])
    assert timeline["competition_id"] == competition.competition_id
