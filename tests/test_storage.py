from __future__ import annotations

import json
import stat

import pytest

from xvote.models import Snapshot
from xvote.storage import SessionManager, SQLiteEventStore

from .helpers import make_competition, make_event


@pytest.mark.parametrize("storage_mode", ["memory", "sqlite"])
def test_checkpoint_restore_recovers_competition_and_events(tmp_path, storage_mode) -> None:
    root = tmp_path / "sessions"
    manager = SessionManager(root)
    session = manager.create(storage_mode=storage_mode, fixture_mode=True)
    competition = make_competition()
    session.add_competition(competition)
    event = make_event(competition, "Alpha")
    session.store.add_event(event)
    session.checkpoint()

    recovered = SessionManager(root)
    notes = recovered.restore()
    assert any(session.session_id in note for note in notes)
    restored = recovered.get(session.session_id)
    assert restored is not None
    assert competition.competition_id in restored.competitions
    assert [item.event_id for item in restored.store.list_events()] == [event.event_id]
    assert restored.fixture_mode is True

    ok, errors = recovered.delete(session.session_id)
    assert ok is True
    assert errors == []
    assert not (root / session.session_id).exists()


def test_checkpoint_files_are_private(tmp_path) -> None:
    manager = SessionManager(tmp_path / "sessions")
    session = manager.create()
    session.checkpoint()
    checkpoint = session.paths.directory / "checkpoint.json"
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert stat.S_IMODE(session.paths.directory.stat().st_mode) == 0o700
    manager.cleanup_all()


def test_restore_removes_orphan_and_unreadable_sessions(tmp_path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    orphan = root / "ses_orphan"
    orphan.mkdir()
    broken = root / "ses_broken"
    broken.mkdir()
    (broken / "checkpoint.json").write_text("{not-json", encoding="utf-8")

    manager = SessionManager(root)
    notes = manager.restore()
    assert any("orphan" in note for note in notes)
    assert any("unreadable" in note for note in notes)
    assert not orphan.exists()
    assert not broken.exists()


def test_list_sessions_returns_a_copy(tmp_path) -> None:
    manager = SessionManager(tmp_path / "sessions")
    session = manager.create()
    listed = manager.list_sessions()
    listed.clear()
    assert manager.get(session.session_id) is session
    manager.cleanup_all()


def test_sqlite_snapshots_preserve_insertion_order_for_identical_timestamps(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite3")
    created = "2026-01-01T00:00:00+00:00"
    first = Snapshot(
        snapshot_id="snp_first",
        competition_id="cmp_1",
        created_at=created,
        ruleset_version="rules",
        view="confirmed",
    )
    second = Snapshot(
        snapshot_id="snp_second",
        competition_id="cmp_1",
        created_at=created,
        ruleset_version="rules",
        view="confirmed",
    )
    store.add_snapshot(second)
    store.add_snapshot(first)
    assert [item.snapshot_id for item in store.list_snapshots("cmp_1")] == [
        "snp_second",
        "snp_first",
    ]
    store.close()


def test_checkpoint_json_is_valid_and_contains_no_credentials(tmp_path) -> None:
    manager = SessionManager(tmp_path / "sessions")
    session = manager.create()
    competition = make_competition()
    session.add_competition(competition)
    session.checkpoint()
    payload = json.loads((session.paths.directory / "checkpoint.json").read_text())
    serialized = json.dumps(payload)
    assert "XCLI_SESSION_COOKIE" not in serialized
    assert "SCWEET_AUTH_TOKEN" not in serialized
    manager.cleanup_all()
