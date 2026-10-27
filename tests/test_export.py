from __future__ import annotations

import json

import pytest

from xvote.service import AppService, ServiceError


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


def test_leaderboard_exports_are_written_with_all_formats(service, demo, tmp_path) -> None:
    session, competition_id = demo
    for fmt, needle in (("csv", b"Alpha"), ("json", b'"records"'), ("html", b"Alpha")):
        record = service.export(session, competition_id, kind="leaderboard", fmt=fmt)
        path = tmp_path / "exports" / session.session_id / record["filename"]
        content = path.read_bytes()
        assert path.is_file()
        assert needle in content
        if fmt == "csv":
            header = path.read_text(encoding="utf-8-sig").splitlines()[0]
            assert header.startswith("data_label,test_data")
            assert "SYNTHETIC TEST DATA" in content.decode("utf-8-sig")
        if fmt == "html":
            assert "SYNTHETIC TEST DATA — NOT LIVE X RESULTS".encode() in content
            assert b"Limitations" in content
        if fmt == "json":
            payload = json.loads(content)
            assert payload["test_data"] is True
            assert payload["records"]


def test_sensitive_fields_require_explicit_confirmation(service, demo) -> None:
    session, competition_id = demo
    with pytest.raises(ServiceError) as excinfo:
        service.export(
            session,
            competition_id,
            kind="events",
            fmt="json",
            fields=["event_id", "account_id"],
        )
    assert excinfo.value.status == 422
    assert "account_id" in str(excinfo.value)

    record = service.export(
        session,
        competition_id,
        kind="events",
        fmt="json",
        fields=["event_id", "account_id"],
        confirm_sensitive=True,
    )
    assert "account_id" in record["sensitive_included"]
    path = service.export_root / session.session_id / record["filename"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["records"]
    assert "account_id" in payload["records"][0]


def test_csv_export_neutralizes_formula_injection(service, demo) -> None:
    session, competition_id = demo
    session.competitions[competition_id].projects[0].name = "=cmd|' /C calc'!A0"
    record = service.export(session, competition_id, kind="leaderboard", fmt="csv")
    path = service.export_root / session.session_id / record["filename"]
    text = path.read_text(encoding="utf-8-sig")
    assert "'=cmd|' /C calc'!A0" in text


def test_export_records_are_listed_in_overview(service, demo) -> None:
    session, competition_id = demo
    record = service.export(session, competition_id, kind="coverage", fmt="csv")
    overview = service.overview(session, competition_id)
    assert any(item["filename"] == record["filename"] for item in overview["exports"])
    assert record["kind"] == "coverage"
    assert record["format"] == "csv"


def test_unknown_export_kind_or_format_is_rejected(service, demo) -> None:
    session, competition_id = demo
    with pytest.raises(ServiceError) as excinfo:
        service.export(session, competition_id, kind="nonsense", fmt="csv")
    assert excinfo.value.status == 422
    with pytest.raises(ServiceError) as excinfo:
        service.export(session, competition_id, kind="leaderboard", fmt="xml")
    assert excinfo.value.status == 422
