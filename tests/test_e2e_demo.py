from __future__ import annotations

import csv
import io

from xvote.service import AppService


def test_synthetic_demo_acceptance_workflow(tmp_path) -> None:
    service = AppService(export_root=tmp_path / "exports")
    try:
        session, result = service.create_demo_session(collect=True)
        competition_id = result["competition_id"]
        assert result["test_data_banner"] == "SYNTHETIC TEST DATA — NOT LIVE X RESULTS"
        assert result["fixture_mode"] is True

        events = session.store.list_events(competition_id)
        assert {event.source_tool for event in events} <= {
            "synthetic-fixture",
            "synthetic-fixture-replay",
        }
        assert all(event.is_test_data for event in events)
        superseded = [event for event in events if event.superseded_by]
        assert len(superseded) == 1
        duplicate = superseded[0]
        assert duplicate.source_tool == "synthetic-fixture-replay"
        winner = session.store.get_event(duplicate.superseded_by)
        assert winner is not None
        assert winner.dedupe_key == duplicate.dedupe_key
        assert winner.event_id != duplicate.event_id
        assert any(
            record.action == "supersede_observation"
            for record in session.store.list_audit(competition_id)
        )
        assert any(event.eligibility_status == "pending_eligibility_review" for event in events)
        assert any(event.eligibility_status == "ineligible" for event in events)
        assert any(event.verification_status == "pending_review" for event in events)

        polls = session.store.list_polls(competition_id)
        assert polls and all(poll.source_tool and poll.collected_at for poll in polls)
        assert result["manual_import"]["aggregates"] == 1
        coverage = list(session.coverage.values())
        assert any(item.post_metrics for item in coverage)
        assert any(item.post_author_username for item in coverage)

        leaderboard = service.leaderboard(session, competition_id)
        assert {entry.name: entry.score for entry in leaderboard.entries} == {
            "Alpha": 121.0,
            "Beta": 94.0,
            "Gamma": 47.0,
        }
        assert leaderboard.is_test_data is True
        assert leaderboard.total_unresolved == 4

        pending = next(
            event
            for event in events
            if event.verification_status == "pending_review" and event.project_id
        )
        target_name = next(
            project.name
            for project in session.competitions[competition_id].projects
            if project.project_id == pending.project_id
        )
        before = {entry.name: entry.score for entry in leaderboard.entries}
        service.review_event(
            session, competition_id, pending.event_id, action="approve", note="e2e approval"
        )
        reviewed = session.store.get_event(pending.event_id)
        assert reviewed is not None
        assert reviewed.manual_decision is True
        assert reviewed.review_status == "manual_override"
        after = {
            entry.name: entry.score
            for entry in service.leaderboard(session, competition_id).entries
        }
        assert after[target_name] == before[target_name] + 1

        first = service.create_snapshot(session, competition_id)
        second = service.create_snapshot(session, competition_id)
        assert first.snapshot_id != second.snapshot_id
        assert (
            service.compare_snapshots(session, competition_id, second.snapshot_id)[
                "ruleset_changed"
            ]
            is False
        )

        exported = service.export(session, competition_id, kind="leaderboard", fmt="csv")
        path = service.export_root / session.session_id / exported["filename"]
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        assert {row["name"]: float(row["confirmed_votes"]) for row in rows} == after

        result = service.delete_session(session.session_id)
        assert result["ok"] is True
        assert path.exists()
        assert service.sessions.get(session.session_id) is None
    finally:
        service.cleanup_all()
