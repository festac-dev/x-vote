"""Loopback HTTP API and static UI server (standard library only)."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .service import AppService, ServiceError
from .templates import list_templates

log = logging.getLogger("x-vote")

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 10 * 1024 * 1024
RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_MAX = 240


class _Bucket:
    def __init__(self) -> None:
        self.count = 0
        self.window_start = time.monotonic()


_RATE_BUCKETS: dict[str, _Bucket] = {}
_RATE_LOCK = threading.Lock()


def _allow(client: str) -> bool:
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.get(client)
        now = time.monotonic()
        if bucket is None or now - bucket.window_start > RATE_LIMIT_WINDOW:
            _RATE_BUCKETS[client] = _Bucket()
            _RATE_BUCKETS[client].count = 1
            return True
        bucket.count += 1
        return bucket.count <= RATE_LIMIT_MAX


class Request:
    def __init__(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
        headers: Any,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default


class Response:
    def __init__(
        self, status: int, payload: Any, *, raw: bool = False, content_type: str = ""
    ) -> None:
        self.status = status
        self.payload = payload
        self.raw = raw
        self.content_type = content_type


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "x-vote/1.0"
    protocol_version = "HTTP/1.1"
    service: AppService

    # ------------------------------------------------------------ dispatch

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.info("%s %s", self.address_string(), format % args)

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not _allow(self.client_address[0]):
            self._json(429, {"error": {"code": "rate_limited", "message": "too many requests"}})
            return
        if path.startswith("/api/"):
            self._handle_api(method, path, parsed)
        else:
            self._handle_static(method, path)

    def _handle_api(self, method: str, path: str, parsed: urllib.parse.SplitResult) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        body: dict[str, Any] = {}
        if method in {"POST", "PUT"}:
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self._json(
                    415,
                    {"error": {"code": "unsupported_media_type", "message": "JSON body required"}},
                )
                return
            if (self.headers.get("X-Requested-With") or "") != "x-vote":
                self._json(
                    403,
                    {
                        "error": {
                            "code": "missing_client_header",
                            "message": "X-Requested-With: x-vote required",
                        }
                    },
                )
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._json(
                    413,
                    {"error": {"code": "payload_too_large", "message": "request body too large"}},
                )
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(
                    400,
                    {
                        "error": {
                            "code": "invalid_json",
                            "message": "request body is not valid JSON",
                        }
                    },
                )
                return
            if not isinstance(body, dict):
                self._json(
                    400,
                    {
                        "error": {
                            "code": "invalid_json",
                            "message": "request body must be an object",
                        }
                    },
                )
                return
        if method == "DELETE" and (self.headers.get("X-Requested-With") or "") != "x-vote":
            self._json(
                403,
                {
                    "error": {
                        "code": "missing_client_header",
                        "message": "X-Requested-With: x-vote required",
                    }
                },
            )
            return
        request = Request(method, path, query, body, self.headers)
        try:
            response = ROUTES.dispatch(self.service, request)
        except ServiceError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except ValueError as exc:
            self._json(422, {"error": {"code": "invalid_request", "message": str(exc)}})
            return
        except Exception as exc:  # never leak internals or secrets
            log.exception("unhandled API error: %s", type(exc).__name__)
            self._json(
                500, {"error": {"code": "internal_error", "message": "unexpected server error"}}
            )
            return
        if response is None:
            self._json(404, {"error": {"code": "not_found", "message": "endpoint not found"}})
            return
        if response.raw and isinstance(response.payload, (bytes, bytearray)):
            self._raw(response.status, bytes(response.payload), response.content_type)
            return
        self._json(response.status, response.payload)

    def _raw(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", "attachment")
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_static(self, method: str, path: str) -> None:
        if method != "GET":
            self._json(
                405, {"error": {"code": "method_not_allowed", "message": "method not allowed"}}
            )
            return
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        candidate = (WEB_ROOT / relative).resolve()
        try:
            candidate.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self._json(404, {"error": {"code": "not_found", "message": "file not found"}})
            return
        if not candidate.is_file() or candidate.is_symlink():
            self._json(404, {"error": {"code": "not_found", "message": "file not found"}})
            return
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix in {".html", ".css", ".js"}:
            media_type += "; charset=utf-8"
        payload = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers()
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )


class Routes:
    def __init__(self) -> None:
        self.table: list[
            tuple[
                str,
                re.Pattern[str],
                Callable[[AppService, Request, re.Match[str]], Response | None],
            ]
        ] = []

    def add(
        self,
        method: str,
        pattern: str,
        handler: Callable[[AppService, Request, re.Match[str]], Response | None],
    ) -> None:
        self.table.append((method, re.compile("^" + pattern + "$"), handler))

    def dispatch(self, service: AppService, request: Request) -> Response | None:
        for method, pattern, handler in self.table:
            if method != request.method:
                continue
            match = pattern.match(request.path)
            if match:
                return handler(service, request, match)
        return None


ROUTES = Routes()


def route(method: str, pattern: str):
    def decorator(handler):
        ROUTES.add(method, pattern, handler)
        return handler

    return decorator


def _session_get(service: AppService, sid: str):
    return service.require_session(sid)


def _project_name(competition, project_id: str) -> str:
    project = competition.project_by_id(project_id)
    return project.name if project else ""


# ------------------------------------------------------------------ routes


@route("GET", r"/api/health")
def health(service: AppService, request: Request, match: re.Match[str]) -> Response:
    return Response(200, {"status": "ok", "version": "1.0.0", "ai_mode": service.config.ai_mode})


@route("GET", r"/api/templates")
def templates(service: AppService, request: Request, match: re.Match[str]) -> Response:
    return Response(200, list_templates())


@route("GET", r"/api/adapters")
def adapters(service: AppService, request: Request, match: re.Match[str]) -> Response:
    return Response(
        200,
        {"adapters": service.adapter_matrix(), "preference_order": service.config.preference_order},
    )


@route("GET", r"/api/settings")
def settings(service: AppService, request: Request, match: re.Match[str]) -> Response:
    return Response(
        200,
        {
            "acquisition": service.config.public_dict(),
            "adapters": service.adapter_matrix(),
            "session_root": str(service.sessions.root),
            "export_root": str(service.export_root),
            "sessions": [
                {
                    "session_id": session.session_id,
                    "created_at": session.created_at,
                    "fixture_mode": session.fixture_mode,
                }
                for session in service.sessions.list_sessions()
            ],
            "credentials_note": (
                "Credentials are read from environment variables only and never serialized."
            ),
        },
    )


@route("POST", r"/api/sessions")
def create_session(service: AppService, request: Request, match: re.Match[str]) -> Response:
    storage_mode = str(request.body.get("storage_mode") or "auto")
    session = service.create_session(
        storage_mode=storage_mode, fixture_mode=bool(request.body.get("fixture_mode"))
    )
    return Response(
        201,
        {
            "session": {
                "session_id": session.session_id,
                "created_at": session.created_at,
                "storage_mode": session.storage_mode,
            }
        },
    )


@route("POST", r"/api/demo")
def demo(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session, result = service.create_demo_session(collect=True)
    return Response(
        201,
        {
            "session_id": session.session_id,
            "banner": result.get("test_data_banner", "SYNTHETIC TEST DATA — NOT LIVE X RESULTS"),
            "result": result,
            "note": "Synthetic test data only; never live X results.",
        },
    )


@route("POST", r"/api/cleanup")
def cleanup_all(service: AppService, request: Request, match: re.Match[str]) -> Response:
    if not request.body.get("confirm"):
        raise ServiceError(
            "cleanup requires confirm=true", status=428, code="confirmation_required"
        )
    results = service.cleanup_all()
    return Response(
        200, {"results": results, "note": "Exported files are outside cleanup and remain on disk."}
    )


@route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/overview")
def overview(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    return Response(200, service.overview(session, request.q("competition_id") or None))


@route("DELETE", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)")
def delete_session(service: AppService, request: Request, match: re.Match[str]) -> Response:
    return Response(200, service.delete_session(match.group("sid")))


@route("POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions")
def create_competition(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    config = request.body.get("config")
    if not isinstance(config, dict):
        raise ServiceError("config object is required", status=422, code="invalid_configuration")
    competition = service.create_competition(
        session, config, confirmed=bool(request.body.get("confirmed"))
    )
    return Response(201, {"competition": competition.to_dict()})


@route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions")
def list_competitions(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    return Response(
        200,
        {
            "active_competition_id": session.active_competition_id,
            "competitions": [
                competition.to_dict() for competition in session.competitions.values()
            ],
        },
    )


@route("PUT", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)")
def update_competition(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    patch = request.body.get("config")
    if not isinstance(patch, dict):
        raise ServiceError("config object is required", status=422, code="invalid_configuration")
    competition = service.update_competition(session, match.group("cid"), patch)
    return Response(200, {"competition": competition.to_dict()})


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/status",
)
def set_competition_status(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    competition = service.set_competition_status(
        session, match.group("cid"), str(request.body.get("status") or "")
    )
    return Response(200, {"competition": competition.to_dict()})


@route("POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/interpret")
def interpret(service: AppService, request: Request, match: re.Match[str]) -> Response:
    from .interpretation import interpret_instruction

    instruction = str(request.body.get("instruction") or "")
    draft = interpret_instruction(
        instruction, competition_name=str(request.body.get("competition_name") or "")
    )
    return Response(200, draft)


@route(
    "POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/collect"
)
def collect(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    methods = request.body.get("methods")
    if methods is not None and not isinstance(methods, list):
        raise ServiceError("methods must be a list", status=422, code="invalid_request")
    report = service.collect(
        session,
        match.group("cid"),
        methods=[str(method) for method in methods] if methods else None,
        run_agents=bool(request.body.get("run_agents", True)),
    )
    return Response(200, report)


@route(
    "POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/import"
)
def import_manual(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    payload = request.body.get("payload")
    if not isinstance(payload, str) or not payload:
        raise ServiceError("payload text is required", status=422, code="invalid_import")
    result = service.import_manual(
        session,
        match.group("cid"),
        payload=payload,
        format_hint=str(request.body.get("format") or ""),
        source_label=str(request.body.get("source_label") or "manual import"),
        source_type=str(request.body.get("source_type") or ""),
    )
    return Response(200, result)


@route(
    "POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/ocr-text"
)
def ocr_text(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    result = service.import_ocr_text(
        session,
        match.group("cid"),
        text=str(request.body.get("text") or ""),
        confidence=str(request.body.get("confidence") or "low"),
        evidence_reference=str(request.body.get("evidence_reference") or ""),
        source_label=str(request.body.get("source_label") or "ocr evidence"),
    )
    return Response(200, result)


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/ocr-image",
)
def ocr_image(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    result = service.upload_ocr_image(
        session,
        match.group("cid"),
        image_base64=str(request.body.get("image_base64") or ""),
        confidence=str(request.body.get("confidence") or "low"),
        source_label=str(request.body.get("source_label") or "ocr upload"),
    )
    return Response(200, result)


@route(
    "GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/events"
)
def list_events(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    competition = service.require_competition(session, match.group("cid"))
    events = session.store.list_events(competition.competition_id)
    status = request.q("status")
    method = request.q("method")
    project = request.q("project")
    search = request.q("search").casefold()
    include_superseded = request.q("include_superseded") == "true"
    filtered = []
    for event in events:
        if event.superseded_by and not include_superseded:
            continue
        if status and status not in {event.eligibility_status, event.verification_status}:
            continue
        if method and event.voting_method != method:
            continue
        if project and event.project_id != project:
            continue
        if search:
            haystack = " ".join(
                [
                    str(event.normalized_value.get("text_excerpt") or ""),
                    str(event.normalized_value.get("author_username") or ""),
                    event.event_id,
                    event.decision_reason,
                ]
            ).casefold()
            if search not in haystack:
                continue
        filtered.append(event)
    filtered.sort(key=lambda event: (event.event_timestamp or "", event.event_id), reverse=True)
    try:
        page = max(1, int(request.q("page", "1")))
        page_size = min(200, max(1, int(request.q("page_size", "50"))))
    except ValueError as exc:
        raise ServiceError(
            "pagination values must be integers", status=422, code="invalid_request"
        ) from exc
    start = (page - 1) * page_size
    selected = filtered[start : start + page_size]
    return Response(
        200,
        {
            "total": len(filtered),
            "page": page,
            "page_size": page_size,
            "ruleset_version": competition.ruleset_version,
            "stale_ruleset_events": sum(
                1 for event in filtered if event.ruleset_version != competition.ruleset_version
            ),
            "events": [
                {
                    **event.to_dict(),
                    "project_name": _project_name(competition, event.project_id),
                    "evidence_excerpt": str(event.normalized_value.get("text_excerpt") or "")[:400],
                    "agent_suggestion": event.normalized_value.get("classification_suggestion"),
                }
                for event in selected
            ],
        },
    )


@route(
    "GET",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/events/(?P<eid>[A-Za-z0-9_\-]+)",
)
def get_event(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    event = session.store.get_event(match.group("eid"))
    if not event or event.competition_id != match.group("cid"):
        raise ServiceError("event not found", status=404, code="event_not_found")
    return Response(
        200,
        {
            "event": event.to_dict(),
            "audit": [
                record.to_dict()
                for record in session.store.list_audit(match.group("cid"))
                if record.event_id == event.event_id
            ],
        },
    )


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/events/(?P<eid>[A-Za-z0-9_\-]+)/review",
)
def review_event(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    result = service.review_event(
        session,
        match.group("cid"),
        match.group("eid"),
        action=str(request.body.get("action") or ""),
        project_id=str(request.body.get("project_id") or ""),
        note=str(request.body.get("note") or ""),
    )
    return Response(200, result)


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/bulk-review",
)
def bulk_review(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    event_ids = request.body.get("event_ids")
    if not isinstance(event_ids, list):
        raise ServiceError("event_ids must be a list", status=422, code="invalid_request")
    result = service.bulk_review(
        session,
        match.group("cid"),
        event_ids=[str(item) for item in event_ids],
        action=str(request.body.get("action") or ""),
        confirm=bool(request.body.get("confirm")),
        note=str(request.body.get("note") or ""),
    )
    return Response(200, result)


@route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/polls")
def list_polls(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    competition = service.require_competition(session, match.group("cid"))
    polls = session.store.list_polls(competition.competition_id)
    return Response(
        200,
        {
            "polls": [
                {
                    **poll.to_dict(),
                    "project_name": _project_name(competition, poll.project_id),
                }
                for poll in polls
            ],
            "limitations": [
                "Poll aggregates are anonymous; individual voters are not observable.",
                "Aggregates and individual events are stored and displayed separately.",
            ],
        },
    )


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/polls/(?P<aid>[A-Za-z0-9_\-]+)/review",
)
def review_poll(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    result = service.review_poll(
        session,
        match.group("cid"),
        match.group("aid"),
        action=str(request.body.get("action") or ""),
        note=str(request.body.get("note") or ""),
    )
    return Response(200, result)


@route(
    "GET",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/leaderboard",
)
def leaderboard(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    view = request.q("view", "confirmed")
    board = service.leaderboard(session, match.group("cid"), view=view)
    return Response(200, {"leaderboard": board.to_dict()})


@route(
    "GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/timeline"
)
def timeline(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    return Response(200, service.timeline(session, match.group("cid")))


@route(
    "GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/snapshots"
)
def list_snapshots(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    snapshots = session.store.list_snapshots(match.group("cid"))
    return Response(200, {"snapshots": [snapshot.to_dict() for snapshot in snapshots]})


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/snapshots",
)
def create_snapshot(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    snapshot = service.create_snapshot(session, match.group("cid"))
    return Response(201, {"snapshot": snapshot.to_dict()})


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/snapshots/(?P<snp>[A-Za-z0-9_\-]+)/compare",
)
def compare_snapshot(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    return Response(200, service.compare_snapshots(session, match.group("cid"), match.group("snp")))


@route(
    "POST",
    r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/agents/run",
)
def run_agents(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    tasks = service.execute_agents(session, match.group("cid"))
    return Response(200, {"tasks": tasks})


@route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/agents")
def list_agents(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    return Response(200, {"tasks": session.agent_tasks})


@route("POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/agents/cancel")
def cancel_agents(service: AppService, request: Request, match: re.Match[str]) -> Response:
    return Response(200, service.cancel_agents(match.group("sid")))


@route("POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/agents/pause")
def pause_agents(service: AppService, request: Request, match: re.Match[str]) -> Response:
    paused = bool(request.body.get("paused", True))
    return Response(200, service.set_agents_paused(match.group("sid"), paused=paused))


@route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/coverage")
def coverage(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    return Response(200, {"coverage": service.coverage_report(session)})


@route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/export-fields")
def export_fields(service: AppService, request: Request, match: re.Match[str]) -> Response:
    return Response(200, service.export_fields(request.q("kind", "leaderboard")))


@route(
    "POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/competitions/(?P<cid>[A-Za-z0-9_\-]+)/export"
)
def export(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    fields = request.body.get("fields")
    if fields is not None and not isinstance(fields, list):
        raise ServiceError("fields must be a list", status=422, code="invalid_request")
    record = service.export(
        session,
        match.group("cid"),
        kind=str(request.body.get("kind") or "leaderboard"),
        fmt=str(request.body.get("format") or "csv"),
        fields=[str(field_name) for field_name in fields] if fields else None,
        confirm_sensitive=bool(request.body.get("confirm_sensitive")),
        view=str(request.body.get("view") or "confirmed"),
    )
    return Response(
        201,
        {
            "export": record,
            "note": (
                "Exported files are outside session cleanup; they are never uploaded automatically."
            ),
        },
    )


@route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_\-]+)/exports/(?P<name>[A-Za-z0-9_.\-]+)")
def download_export(service: AppService, request: Request, match: re.Match[str]) -> Response:
    session = _session_get(service, match.group("sid"))
    root = (service.export_root / session.session_id).resolve()
    candidate = (root / match.group("name")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ServiceError("export not found", status=404, code="export_not_found") from None
    if not candidate.is_file() or candidate.is_symlink():
        raise ServiceError("export not found", status=404, code="export_not_found")
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if candidate.suffix in {".csv", ".json", ".html"}:
        content_type += "; charset=utf-8"
    return Response(200, candidate.read_bytes(), raw=True, content_type=content_type)


def serve(service: AppService | None = None, host: str = "", port: int = 0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = service or AppService()
    host = host or os.environ.get("XVOTE_HOST") or "127.0.0.1"
    port = port or int(os.environ.get("XVOTE_PORT", "8765"))
    recovery = service.sessions.restore()
    for note in recovery:
        log.info("session recovery: %s", note)
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    log.info("x-vote listening on http://%s:%s (loopback by default)", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested; cleaning temporary sessions")
    finally:
        server.server_close()
        cancelled = service.cancel_all_agents()
        if cancelled:
            log.info("cancellation requested for %s active agent plan(s)", cancelled)
        results = service.sessions.close()
        failures = {sid: result for sid, result in results.items() if not result["ok"]}
        log.info("cleanup complete; failures: %s", failures or "none")
