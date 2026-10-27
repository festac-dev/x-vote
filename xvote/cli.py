"""Command line interface for x-vote."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .service import AppService

    service = AppService()
    session, result = service.create_demo_session(collect=not args.no_collect)
    payload: dict[str, Any] = {}
    try:
        board = service.leaderboard(session, result["competition_id"], view="confirmed")
        payload = {
            "session_id": session.session_id,
            "banner": result.get("test_data_banner"),
            "leaderboard": [
                {"rank": entry.rank, "project": entry.name, "score": entry.score}
                for entry in board.entries
            ],
            "note": "Synthetic test data only; not live X results.",
        }
    finally:
        cleanup = service.delete_session(session.session_id)
        if payload:
            payload["cleanup"] = cleanup
    _print(payload)
    return 0


def cmd_interpret(args: argparse.Namespace) -> int:
    from .interpretation import interpret_instruction

    _print(interpret_instruction(args.instruction, competition_name=args.name))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from .service import AppService

    service = AppService()
    if args.action == "list":
        _print(
            [
                {
                    "session_id": session.session_id,
                    "created_at": session.created_at,
                    "fixture_mode": session.fixture_mode,
                }
                for session in service.sessions.list_sessions()
            ]
        )
    elif args.action == "delete":
        _print(service.delete_session(args.session_id))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from .service import AppService

    service = AppService()
    _print({"results": service.cleanup_all(), "note": "Exports live outside session cleanup."})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xvote", description="Universal X voting checker (open-source acquisition)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the local loopback web UI and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_serve)

    demo = sub.add_parser("demo", help="run the built-in synthetic demo end to end")
    demo.add_argument(
        "--no-collect", action="store_true", help="create the demo session without collecting"
    )
    demo.set_defaults(func=cmd_demo)

    interpret = sub.add_parser(
        "interpret",
        help="preview the deterministic configuration draft from a plain-language instruction",
    )
    interpret.add_argument("instruction")
    interpret.add_argument("--name", default="")
    interpret.set_defaults(func=cmd_interpret)

    sessions = sub.add_parser("sessions", help="list or delete temporary sessions")
    sessions.add_argument("action", choices=["list", "delete"])
    sessions.add_argument("session_id", nargs="?")
    sessions.set_defaults(func=cmd_sessions)

    cleanup = sub.add_parser("cleanup", help="close and delete all temporary sessions")
    cleanup.set_defaults(func=cmd_cleanup)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from .config import load_env_file

    try:
        load_env_file()
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "sessions" and args.action == "delete" and not args.session_id:
        parser.error("sessions delete requires a session_id")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
