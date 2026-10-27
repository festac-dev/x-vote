from __future__ import annotations

import pytest

from xvote.models import Source, VotingMethod
from xvote.ranking import build_leaderboard
from xvote.service import AppService, ServiceError

from .helpers import make_competition


@pytest.fixture
def service(tmp_path):
    instance = AppService(export_root=tmp_path / "exports")
    try:
        yield instance
    finally:
        instance.cleanup_all()


@pytest.fixture
def demo(service):
    session, result = service.create_demo_session(collect=True)
    return session, result["competition_id"]


def test_demo_overview_reports_unassigned_and_unresolved(service, demo) -> None:
    session, competition_id = demo
    overview = service.overview(session, competition_id)
    assert overview["total_observed_activity"] == 282
    assert overview["confirmed_votes"] == 262
    assert overview["provisional_votes"] == 17
    assert overview["pending_review"] == 3
    assert overview["ambiguous"] == 1
    assert overview["rejected_or_late_activity"] == 1
    assert overview["test_data_banner"] == "SYNTHETIC TEST DATA — NOT LIVE X RESULTS"

    leaderboard = service.leaderboard(session, competition_id)
    assert {entry.name: entry.score for entry in leaderboard.entries} == {
        "Alpha": 121.0,
        "Beta": 94.0,
        "Gamma": 47.0,
    }
    assert leaderboard.unassigned_events == 1
    assert leaderboard.unassigned_pending_review == 1
    assert leaderboard.total_unresolved == 4


def test_manual_approval_resolves_unresolved_and_updates_score(service, demo) -> None:
    session, competition_id = demo
    before = service.leaderboard(session, competition_id)
    target = next(
        event
        for event in session.store.list_events(competition_id)
        if event.project_id
        and event.verification_status == "pending_review"
        and event.eligibility_status == "pending_eligibility_review"
    )
    target_score = next(
        entry.score for entry in before.entries if entry.project_id == target.project_id
    )
    result = service.review_event(
        session, competition_id, target.event_id, action="approve", note="unit test"
    )
    assert result["event"]["manual_decision"] is True
    assert result["event"]["verification_status"] == "verified"
    assert result["event"]["eligibility_status"] == "eligible"
    assert result["audit"]["action"] == "manual_approve"
    assert session.store.list_audit(competition_id)[-1].note == "unit test"

    after = service.leaderboard(session, competition_id)
    assert after.total_unresolved == before.total_unresolved - 1
    assert (
        next(entry.score for entry in after.entries if entry.project_id == target.project_id)
        == target_score + 1
    )


def test_result_status_confirmation_and_finalized_collection_block(service, demo) -> None:
    session, competition_id = demo
    with pytest.raises(ServiceError) as invalid:
        service.set_competition_status(session, competition_id, "published")
    assert invalid.value.code == "invalid_status"

    updated = service.set_competition_status(session, competition_id, "finalized")
    assert updated.status == "finalized"
    assert service.overview(session, competition_id)["competition"]["status"] == "finalized"
    audit = session.store.list_audit(competition_id)[-1]
    assert audit.action == "competition_status_changed"
    assert audit.before == {"status": "collecting"}
    assert audit.after == {"status": "finalized"}

    with pytest.raises(ServiceError) as blocked:
        service.collect(session, competition_id, run_agents=False)
    assert blocked.value.code == "competition_finalized"

    reopened = service.set_competition_status(session, competition_id, "collecting")
    assert reopened.status == "collecting"


def test_ruleset_update_recalculates_without_recollection(service, demo) -> None:
    session, competition_id = demo
    events_before = session.store.list_events(competition_id)
    old_ruleset = session.competitions[competition_id].ruleset_version
    updated = service.update_competition(
        session,
        competition_id,
        {
            "ranking": {
                "mode": "confirmed",
                "weights": {},
                "custom_formula": "",
                "combine_confirmed_with_aggregates": False,
            }
        },
    )
    assert updated.ruleset_version != old_ruleset
    events_after = session.store.list_events(competition_id)
    assert len(events_after) == len(events_before)
    assert all(event.ruleset_version == updated.ruleset_version for event in events_after)

    leaderboard = build_leaderboard(
        updated,
        events_after,
        session.store.list_polls(competition_id),
        session.coverage,
    )
    assert {entry.name: entry.score for entry in leaderboard.entries} == {
        "Alpha": 1.0,
        "Beta": 4.0,
        "Gamma": 2.0,
    }
    assert leaderboard.aggregates_ranked_separately is True
    assert leaderboard.total_confirmed == 262
    assert leaderboard.confirmed_individual_total == 7
    assert leaderboard.confirmed_aggregate_total == 255


def test_live_collection_requires_authorized_use_acknowledgement(service, monkeypatch) -> None:
    monkeypatch.delenv("XVOTE_AUTHORIZED_USE_ACK", raising=False)
    session = service.create_session()
    source = Source(
        source_type="x_post",
        post_id="1234567890",
        url="https://x.com/example/status/1234567890",
        methods=[VotingMethod.REPLY.value],
    )
    competition = make_competition(sources=[source])
    service.create_competition(session, competition.to_dict(), confirmed=True)
    with pytest.raises(ServiceError) as excinfo:
        service.collect(session, competition.competition_id)
    assert excinfo.value.status == 428
    assert excinfo.value.code == "authorized_use_not_acknowledged"

    monkeypatch.setenv("XVOTE_AUTHORIZED_USE_ACK", "yes")
    assert service.config.authorized_use_acknowledged is True


def test_snapshot_ids_are_unique_and_compare_uses_previous(service, demo) -> None:
    session, competition_id = demo
    first = service.create_snapshot(session, competition_id)
    second = service.create_snapshot(session, competition_id)
    assert first.snapshot_id != second.snapshot_id
    comparison = service.compare_snapshots(session, competition_id, second.snapshot_id)
    assert comparison["count_changes"] == {}
    assert comparison["rank_changes"] == {}
    assert comparison["ruleset_changed"] is False


def test_competition_patch_rejects_invalid_configuration(service, demo) -> None:
    session, competition_id = demo
    with pytest.raises(ServiceError) as excinfo:
        service.update_competition(
            session, competition_id, {"ranking": {"mode": "definitely-not-a-mode"}}
        )
    assert excinfo.value.status == 422
    assert excinfo.value.code == "invalid_configuration"
