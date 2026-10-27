"""Temporary, session-scoped storage with honest cleanup reporting."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from .models import (
    AuditRecord,
    CollectionStatus,
    Competition,
    NormalizedEvent,
    PollAggregate,
    Snapshot,
    SourceCoverage,
    isoformat,
    utc_now,
)


class StorageError(RuntimeError):
    pass


class EventStore(ABC):
    @abstractmethod
    def add_event(self, event: NormalizedEvent) -> None: ...

    @abstractmethod
    def get_event(self, event_id: str) -> NormalizedEvent | None: ...

    @abstractmethod
    def list_events(self, competition_id: str | None = None) -> list[NormalizedEvent]: ...

    @abstractmethod
    def add_poll(self, poll: PollAggregate) -> None: ...

    @abstractmethod
    def list_polls(self, competition_id: str | None = None) -> list[PollAggregate]: ...

    @abstractmethod
    def add_audit(self, record: AuditRecord) -> None: ...

    @abstractmethod
    def list_audit(self, competition_id: str | None = None) -> list[AuditRecord]: ...

    @abstractmethod
    def add_snapshot(self, snapshot: Snapshot) -> None: ...

    @abstractmethod
    def list_snapshots(self, competition_id: str | None = None) -> list[Snapshot]: ...

    @abstractmethod
    def count_events(self) -> int: ...

    @abstractmethod
    def close(self) -> None: ...


class MemoryEventStore(EventStore):
    def __init__(self) -> None:
        self._events: dict[str, NormalizedEvent] = {}
        self._polls: dict[str, PollAggregate] = {}
        self._audit: list[AuditRecord] = []
        self._snapshots: list[Snapshot] = []
        self._lock = threading.RLock()

    def add_event(self, event: NormalizedEvent) -> None:
        with self._lock:
            self._events[event.event_id] = event

    def get_event(self, event_id: str) -> NormalizedEvent | None:
        with self._lock:
            return self._events.get(event_id)

    def list_events(self, competition_id: str | None = None) -> list[NormalizedEvent]:
        with self._lock:
            values = list(self._events.values())
        if competition_id:
            values = [event for event in values if event.competition_id == competition_id]
        return values

    def add_poll(self, poll: PollAggregate) -> None:
        with self._lock:
            self._polls[poll.aggregate_id] = poll

    def list_polls(self, competition_id: str | None = None) -> list[PollAggregate]:
        with self._lock:
            values = list(self._polls.values())
        if competition_id:
            values = [poll for poll in values if poll.competition_id == competition_id]
        return values

    def add_audit(self, record: AuditRecord) -> None:
        with self._lock:
            self._audit.append(record)

    def list_audit(self, competition_id: str | None = None) -> list[AuditRecord]:
        with self._lock:
            values = list(self._audit)
        if competition_id:
            values = [record for record in values if record.competition_id == competition_id]
        return values

    def add_snapshot(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshots.append(snapshot)

    def list_snapshots(self, competition_id: str | None = None) -> list[Snapshot]:
        with self._lock:
            values = list(self._snapshots)
        if competition_id:
            values = [snap for snap in values if snap.competition_id == competition_id]
        return values

    def count_events(self) -> int:
        return len(self._events)

    def close(self) -> None:
        with self._lock:
            self._events.clear()
            self._polls.clear()
            self._audit.clear()
            self._snapshots.clear()


class SQLiteEventStore(EventStore):
    """Temp SQLite repository. All temporary and removed by the cleanup manager."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
              event_id TEXT PRIMARY KEY,
              competition_id TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_comp ON events(competition_id);
            CREATE TABLE IF NOT EXISTS polls (
              aggregate_id TEXT PRIMARY KEY,
              competition_id TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
              audit_id TEXT PRIMARY KEY,
              competition_id TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
              snapshot_id TEXT PRIMARY KEY,
              competition_id TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            """
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    _TABLES = {"events", "polls", "audit", "snapshots"}
    _KEYS = {"event_id", "aggregate_id", "audit_id", "snapshot_id"}

    def _upsert(
        self, table: str, key_name: str, key: str, competition_id: str, payload: dict
    ) -> None:
        if table not in self._TABLES or key_name not in self._KEYS:
            raise ValueError("unknown table or key")
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table} ({key_name}, competition_id, payload) "  # noqa: S608 - allow-listed identifiers
                "VALUES (?, ?, ?)",
                (key, competition_id, json.dumps(payload, ensure_ascii=False)),
            )

    def _list(self, table: str, competition_id: str | None) -> list[dict[str, Any]]:
        if table not in self._TABLES:
            raise ValueError("unknown table")
        query = f"SELECT payload FROM {table}"  # noqa: S608 - allow-listed identifiers
        params: tuple[Any, ...] = ()
        if competition_id:
            query += " WHERE competition_id = ?"
            params = (competition_id,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def add_event(self, event: NormalizedEvent) -> None:
        self._upsert("events", "event_id", event.event_id, event.competition_id, event.to_dict())

    def get_event(self, event_id: str) -> NormalizedEvent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return NormalizedEvent.from_dict(json.loads(row[0])) if row else None

    def list_events(self, competition_id: str | None = None) -> list[NormalizedEvent]:
        return [NormalizedEvent.from_dict(row) for row in self._list("events", competition_id)]

    def add_poll(self, poll: PollAggregate) -> None:
        self._upsert(
            "polls", "aggregate_id", poll.aggregate_id, poll.competition_id, poll.to_dict()
        )

    def list_polls(self, competition_id: str | None = None) -> list[PollAggregate]:
        return [PollAggregate.from_dict(row) for row in self._list("polls", competition_id)]

    def add_audit(self, record: AuditRecord) -> None:
        self._upsert("audit", "audit_id", record.audit_id, record.competition_id, record.to_dict())

    def list_audit(self, competition_id: str | None = None) -> list[AuditRecord]:
        return [AuditRecord.from_dict(row) for row in self._list("audit", competition_id)]

    def add_snapshot(self, snapshot: Snapshot) -> None:
        self._upsert(
            "snapshots",
            "snapshot_id",
            snapshot.snapshot_id,
            snapshot.competition_id,
            snapshot.to_dict(),
        )

    def list_snapshots(self, competition_id: str | None = None) -> list[Snapshot]:
        query = "SELECT payload FROM snapshots"
        params: tuple[Any, ...] = ()
        if competition_id:
            query += " WHERE competition_id = ?"
            params = (competition_id,)
        query += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [Snapshot(**json.loads(row[0])) for row in rows]

    def count_events(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()


@dataclass
class SessionPaths:
    root: Path
    directory: Path
    evidence: Path

    @classmethod
    def create(cls, root: Path, session_id: str) -> SessionPaths:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        directory = root / session_id
        directory.mkdir(mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        evidence = directory / "evidence"
        evidence.mkdir(mode=0o700)
        return cls(root=root, directory=directory, evidence=evidence)


@dataclass
class Session:
    session_id: str
    paths: SessionPaths
    store: EventStore
    created_at: str = field(default_factory=lambda: isoformat(utc_now()))
    competitions: dict[str, Competition] = field(default_factory=dict)
    active_competition_id: str = ""
    coverage: dict[str, SourceCoverage] = field(default_factory=dict)
    fixture_mode: bool = False
    fixture: dict[str, Any] | None = None
    agent_tasks: list[dict[str, Any]] = field(default_factory=list)
    exports: list[dict[str, Any]] = field(default_factory=list)
    cleanup_status: str = "pending"
    cleanup_errors: list[str] = field(default_factory=list)
    last_collection_at: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def storage_mode(self) -> str:
        return "sqlite" if isinstance(self.store, SQLiteEventStore) else "memory"

    def add_competition(self, competition: Competition) -> None:
        self.competitions[competition.competition_id] = competition
        self.active_competition_id = competition.competition_id
        for source in competition.sources:
            self.coverage.setdefault(
                source.source_id,
                SourceCoverage(
                    source_id=source.source_id,
                    source_type=source.source_type,
                    label=source.label or source.source_type,
                    collection_status=CollectionStatus.NOT_STARTED.value,
                ),
            )

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "active_competition_id": self.active_competition_id,
            "competitions": [comp.to_dict() for comp in self.competitions.values()],
            "coverage": [cov.to_dict() for cov in self.coverage.values()],
            "fixture_mode": self.fixture_mode,
            "fixture": self.fixture,
            "agent_tasks": self.agent_tasks,
            "exports": self.exports,
            "cleanup_status": self.cleanup_status,
            "last_collection_at": self.last_collection_at,
            "storage_mode": self.storage_mode,
        }

    def checkpoint(self) -> None:
        payload = self.to_checkpoint()
        if isinstance(self.store, MemoryEventStore):
            payload["events"] = [event.to_dict() for event in self.store.list_events()]
            payload["polls"] = [poll.to_dict() for poll in self.store.list_polls()]
            payload["audit"] = [record.to_dict() for record in self.store.list_audit()]
            payload["snapshots"] = [snap.to_dict() for snap in self.store.list_snapshots()]
        path = self.paths.directory / "checkpoint.json"
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        temp_path.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def promote_to_sqlite(self) -> None:
        if isinstance(self.store, SQLiteEventStore):
            return
        new_store = SQLiteEventStore(self.paths.directory / "session.sqlite3")
        for event in self.store.list_events():
            new_store.add_event(event)
        for poll in self.store.list_polls():
            new_store.add_poll(poll)
        for record in self.store.list_audit():
            new_store.add_audit(record)
        for snapshot in self.store.list_snapshots():
            new_store.add_snapshot(snapshot)
        self.store.close()
        self.store = new_store


class SessionManager:
    def __init__(self, root: str | Path | None = None) -> None:
        env_root = os.environ.get("XVOTE_SESSION_ROOT", "").strip()
        base = (
            Path(root)
            if root
            else (Path(env_root) if env_root else Path(tempfile.gettempdir()) / "x-vote-sessions")
        )
        self.root = base
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def create(self, *, storage_mode: str = "auto", fixture_mode: bool = False) -> Session:
        import uuid

        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        paths = SessionPaths.create(self.root, session_id)
        if storage_mode == "sqlite":
            store: EventStore = SQLiteEventStore(paths.directory / "session.sqlite3")
        else:
            store = MemoryEventStore()
        session = Session(
            session_id=session_id, paths=paths, store=store, fixture_mode=fixture_mode
        )
        with self._lock:
            self._sessions[session_id] = session
        session.checkpoint()
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def delete(self, session_id: str) -> tuple[bool, list[str]]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if not session:
            return False, ["session not found"]
        errors: list[str] = []
        try:
            session.store.close()
        except Exception as exc:  # cleanup must continue and report
            errors.append(f"close store: {exc}")
        shutil.rmtree(session.paths.directory, ignore_errors=True)
        if session.paths.directory.exists():
            errors.append("session directory could not be fully removed")
        session.cleanup_status = "succeeded" if not errors else "failed"
        session.cleanup_errors = errors
        return not errors, errors

    def restore(self) -> list[str]:
        """Restore sessions from valid checkpoints after an interrupted process.

        Returns human-readable recovery notes. Invalid or unreadable checkpoints
        are removed rather than silently ignored.
        """
        notes: list[str] = []
        if not self.root.exists():
            return notes
        for child in self.root.iterdir():
            if not child.is_dir() or not child.name.startswith("ses_"):
                continue
            session_id = child.name
            if self.get(session_id):
                continue
            checkpoint = child / "checkpoint.json"
            if not checkpoint.exists():
                shutil.rmtree(child, ignore_errors=True)
                notes.append(f"removed orphan session directory {session_id}")
                continue
            try:
                payload = json.loads(checkpoint.read_text(encoding="utf-8"))
                competitions = [Competition.from_dict(item) for item in payload["competitions"]]
                coverage = {
                    item["source_id"]: SourceCoverage(**item)
                    for item in payload.get("coverage", [])
                }
                mode = payload.get("storage_mode", "memory")
                if mode == "sqlite" and (child / "session.sqlite3").exists():
                    store: EventStore = SQLiteEventStore(child / "session.sqlite3")
                else:
                    store = MemoryEventStore()
                    for item in payload.get("events", []):
                        store.add_event(NormalizedEvent.from_dict(item))
                    for item in payload.get("polls", []):
                        store.add_poll(PollAggregate.from_dict(item))
                    for item in payload.get("audit", []):
                        store.add_audit(AuditRecord.from_dict(item))
                    for item in payload.get("snapshots", []):
                        store.add_snapshot(Snapshot(**item))
                paths = SessionPaths(root=self.root, directory=child, evidence=child / "evidence")
                paths.evidence.mkdir(mode=0o700, exist_ok=True)
                session = Session(
                    session_id=session_id,
                    paths=paths,
                    store=store,
                    created_at=payload.get("created_at", isoformat(utc_now())),
                    competitions={comp.competition_id: comp for comp in competitions},
                    active_competition_id=payload.get("active_competition_id", ""),
                    coverage=coverage,
                    fixture_mode=bool(payload.get("fixture_mode")),
                    fixture=payload.get("fixture"),
                    agent_tasks=payload.get("agent_tasks", []),
                    exports=payload.get("exports", []),
                    cleanup_status=payload.get("cleanup_status", "pending"),
                    last_collection_at=payload.get("last_collection_at", ""),
                )
                with self._lock:
                    self._sessions[session_id] = session
                notes.append(f"recovered session {session_id}")
            except Exception as exc:
                shutil.rmtree(child, ignore_errors=True)
                notes.append(f"discarded unreadable session checkpoint {session_id}: {exc}")
        return notes

    def cleanup_all(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for session_id in [session.session_id for session in self.list_sessions()]:
            ok, errors = self.delete(session_id)
            results[session_id] = {"ok": ok, "errors": errors}
        return results

    def close(self) -> dict[str, Any]:
        return self.cleanup_all()


def stale_session_cleanup(root: str | Path, older_than_seconds: int = 86400) -> list[str]:
    from datetime import datetime

    base = Path(root)
    removed: list[str] = []
    if not base.exists():
        return removed
    now = datetime.now(UTC)
    for child in base.iterdir():
        if not child.is_dir() or not child.name.startswith("ses_"):
            continue
        try:
            age = (now - datetime.fromtimestamp(child.stat().st_mtime, UTC)).total_seconds()
        except OSError:
            continue
        if age >= older_than_seconds:
            shutil.rmtree(child, ignore_errors=True)
            if not child.exists():
                removed.append(child.name)
    return removed


def find_evidence_files(directory: Path) -> Iterable[Path]:
    for path in directory.rglob("*"):
        if path.is_file():
            yield path
