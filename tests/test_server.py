from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from xvote.server import ApiHandler
from xvote.service import AppService


@pytest.fixture
def http_server(tmp_path):
    service = AppService(export_root=tmp_path / "exports")
    ApiHandler.service = service
    server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    session = requests.Session()
    session.headers.update({"X-Requested-With": "x-vote"})
    try:
        yield base, session, service
    finally:
        server.shutdown()
        server.server_close()
        service.cleanup_all()
        thread.join(timeout=5)


def test_health_and_static_ui(http_server) -> None:
    base, http, _service = http_server
    health = http.get(base + "/api/health", timeout=10)
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    index = http.get(base + "/", timeout=10)
    assert index.status_code == 200
    assert "x-vote" in index.text
    assert index.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in index.headers["Content-Security-Policy"]

    app_js = http.get(base + "/app.js", timeout=10)
    assert app_js.status_code == 200
    assert "javascript" in app_js.headers["Content-Type"]


def test_templates_endpoint_is_public_and_safe(http_server) -> None:
    base, http, _service = http_server
    response = http.get(base + "/api/templates", timeout=10)
    assert response.status_code == 200
    payload = response.json()
    assert payload["templates"]
    assert "defaults only" in payload["note"]
    assert {item["id"] for item in payload["templates"]} >= {"native_poll", "reply_comment"}


def test_mutations_require_csrf_header(http_server) -> None:
    base, _http, _service = http_server
    response = requests.post(base + "/api/demo", json={}, timeout=10)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "missing_client_header"


def test_demo_workflow_over_http(http_server) -> None:
    base, http, _service = http_server
    demo = http.post(base + "/api/demo", json={}, timeout=30)
    assert demo.status_code == 201
    payload = demo.json()
    assert payload["banner"] == "SYNTHETIC TEST DATA — NOT LIVE X RESULTS"
    session_id = payload["session_id"]
    competition_id = payload["result"]["competition_id"]

    overview = http.get(base + f"/api/sessions/{session_id}/overview", timeout=10)
    assert overview.status_code == 200
    assert overview.json()["confirmed_votes"] == 262

    board = http.get(
        base + f"/api/sessions/{session_id}/competitions/{competition_id}/leaderboard",
        timeout=10,
    )
    assert board.status_code == 200
    leaderboard = board.json()["leaderboard"]
    assert {entry["name"]: entry["score"] for entry in leaderboard["entries"]} == {
        "Alpha": 121.0,
        "Beta": 94.0,
        "Gamma": 47.0,
    }
    assert leaderboard["total_unresolved"] == 4

    invalid = http.get(
        base + f"/api/sessions/{session_id}/competitions/{competition_id}/leaderboard",
        params={"view": "nonsense"},
        timeout=10,
    )
    assert invalid.status_code == 422

    exported = http.post(
        base + f"/api/sessions/{session_id}/competitions/{competition_id}/export",
        json={"kind": "leaderboard", "format": "csv"},
        timeout=10,
    )
    assert exported.status_code == 201
    record = exported.json()["export"]
    download = http.get(
        base + f"/api/sessions/{session_id}/exports/{record['filename']}", timeout=10
    )
    assert download.status_code == 200
    assert "Alpha" in download.text

    traversal = http.get(base + "/exports/..%2f..%2fcheckpoint.json", timeout=10)
    assert traversal.status_code == 404

    finalized = http.post(
        base + f"/api/sessions/{session_id}/competitions/{competition_id}/status",
        json={"status": "finalized"},
        timeout=10,
    )
    assert finalized.status_code == 200
    assert finalized.json()["competition"]["status"] == "finalized"
    blocked = http.post(
        base + f"/api/sessions/{session_id}/competitions/{competition_id}/collect",
        json={"run_agents": False},
        timeout=10,
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "competition_finalized"
