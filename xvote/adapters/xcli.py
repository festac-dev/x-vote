"""x-cli adapter (Tier 2/3, session cookie, strictly read-only).

Wraps the Go binary `x` using fixed, non-shell commands with JSONL output. The
session cookie is read from the configured environment variable, written to a
0600 file inside the temporary session directory, and deleted at cleanup. It is
never placed on the command line or logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import XCliSettings
from .base import (
    AcquisitionAdapter,
    CollectionError,
    CredentialsMissing,
    NormalizedPoll,
    NormalizedReply,
    SourceUnavailableError,
)

MAX_OUTPUT_BYTES = 8 * 1024 * 1024


class XCliAdapter(AcquisitionAdapter):
    name = "x-cli"
    auth_tier = "session"
    license_note = (
        "Wrap only according to the tool's own license and X's terms; read-only commands, "
        "authorized sessions only."
    )

    def __init__(
        self,
        settings: XCliSettings,
        session_dir: Path,
        *,
        runner: Any = None,
    ) -> None:
        self.settings = settings
        self.session_dir = session_dir
        self._runner = runner
        self._cookie_file = session_dir / ".xcli_session"

    def capabilities(self) -> dict[str, bool]:
        return {
            "poll": True,
            "replies": True,
            "individual_likers": True,
            "individual_reposters": True,
            "auth_required": True,
        }

    def version_info(self) -> str:
        binary = self._binary()
        if not binary:
            return ""
        return _run_probe([binary, "--version"]) or "unknown (--version produced no output)"

    def command_probe(self) -> dict[str, Any]:
        binary = self._binary()
        if not binary:
            return {}
        help_text = _run_probe([binary, "--help"])
        if not help_text:
            return {
                "probe": (
                    "unknown (--help produced no output; verify the installed version's commands)"
                ),
            }
        lowered = help_text.casefold()
        return {
            command: (True if command in lowered else None)
            for command in ("tweet", "poll", "replies", "likers", "retweeters")
        }

    def _binary(self) -> str:
        if os.path.sep in self.settings.binary:
            return self.settings.binary
        return shutil.which(self.settings.binary) or ""

    def available(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "disabled in acquisition configuration"
        if self._runner is not None:
            return True, ""
        if not self._binary():
            return False, "x-cli binary not found"
        if not os.environ.get(self.settings.session_cookie_env, "").strip():
            return False, "session cookie environment variable is not set"
        return True, ""

    def _write_cookie(self) -> None:
        cookie = os.environ.get(self.settings.session_cookie_env, "").strip()
        if not cookie:
            raise CredentialsMissing("x-cli session cookie is not configured", adapter=self.name)
        try:
            self._cookie_file.write_text(cookie, encoding="utf-8")
            os.chmod(self._cookie_file, 0o600)
        except OSError as exc:
            raise CredentialsMissing(
                f"x-cli session file could not be created: {type(exc).__name__}", adapter=self.name
            ) from exc

    async def _run(self, args: list[str]) -> list[dict[str, Any]]:
        if self._runner is not None:
            payload = await self._runner(args)
            return _parse_json_lines(payload, adapter=self.name)
        binary = self._binary()
        if not binary:
            raise SourceUnavailableError("x-cli binary not found", adapter=self.name)
        self._write_cookie()
        env = {
            **os.environ,
            "X_SESSION_FILE": str(self._cookie_file),
            "XCLI_SESSION_FILE": str(self._cookie_file),
        }
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                *args,
                "-o",
                "jsonl",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.settings.timeout_seconds
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise SourceUnavailableError("x-cli timed out", adapter=self.name) from exc
        except FileNotFoundError as exc:
            raise SourceUnavailableError("x-cli binary not found", adapter=self.name) from exc
        if process.returncode != 0:
            message = "x-cli was rate limited or refused the request"
            stderr_text = _stderr.decode("utf-8", errors="replace").lower()[:400] if _stderr else ""
            if "rate" in stderr_text and "limit" in stderr_text:
                from .base import RateLimitError

                raise RateLimitError(message, adapter=self.name)
            if "unauthorized" in stderr_text or "login" in stderr_text:
                raise CredentialsMissing("x-cli session is not authorized", adapter=self.name)
            raise CollectionError(
                f"x-cli exited with status {process.returncode}", adapter=self.name
            )
        if len(stdout) > MAX_OUTPUT_BYTES:
            raise CollectionError("x-cli output exceeded the size limit", adapter=self.name)
        return _parse_json_lines(stdout.decode("utf-8", errors="replace"), adapter=self.name)

    async def fetch_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        rows = await self._run(["tweet", str(tweet_id)])
        if not rows:
            return None
        return _map_tweet(rows[0], tweet_id, self.name, self.auth_tier)

    async def fetch_replies(self, tweet_id: str, limit: int = -1) -> list[NormalizedReply]:
        args = ["replies", str(tweet_id)]
        if limit >= 0:
            args.extend(["-n", str(limit)])
        rows = await self._run(args)
        replies: list[NormalizedReply] = []
        for row in rows:
            author = row.get("author") or row.get("user") or {}
            reply_id = str(row.get("id") or row.get("tweet_id") or row.get("rest_id") or "")
            if not reply_id:
                continue
            replies.append(
                NormalizedReply(
                    reply_id=reply_id,
                    author_id=str(author.get("id") or row.get("author_id") or ""),
                    author_username=str(author.get("username") or author.get("screen_name") or ""),
                    text=str(row.get("text") or row.get("full_text") or row.get("content") or ""),
                    created_at=_iso(row.get("created_at") or row.get("date")),
                    url=str(row.get("url") or f"https://x.com/i/web/status/{reply_id}"),
                    parent_post_id=str(tweet_id),
                    source_tool=self.name,
                    auth_tier=self.auth_tier,
                    raw_evidence_reference=f"x-cli:replies:{tweet_id}",
                )
            )
            if limit >= 0 and len(replies) >= limit:
                break
        return replies

    async def fetch_poll(self, tweet_id: str) -> NormalizedPoll | None:
        rows = await self._run(["poll", str(tweet_id)])
        options: list[dict[str, Any]] = []
        finished = False
        for row in rows:
            if isinstance(row.get("options"), list):
                for option in row["options"]:
                    label = str(option.get("label") or option.get("choice") or "")
                    votes = _int(option.get("votes", option.get("votesCount", 0)))
                    if label:
                        options.append({"label": label, "votes": votes})
                finished = bool(row.get("finished", False))
            elif row.get("label") or row.get("choice"):
                label = str(row.get("label") or row.get("choice"))
                options.append({"label": label, "votes": _int(row.get("votes", 0))})
                finished = finished or bool(row.get("finished", False))
        if not options:
            return None
        limitations = [
            "Native poll votes are anonymous aggregates; individual voters are not observable."
        ]
        if not finished:
            limitations.append("Poll was still open when observed; counts may change.")
        return NormalizedPoll(
            post_id=str(tweet_id),
            options=options,
            total_votes=sum(option["votes"] for option in options),
            finished=finished,
            collected_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source_tool=self.name,
            auth_tier=self.auth_tier,
            raw_evidence_reference=f"x-cli:poll:{tweet_id}",
            url=f"https://x.com/i/web/status/{tweet_id}",
            exact_counts=True,
            limitations=limitations,
        )

    async def fetch_likers(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        return await self._fetch_accounts("likers", tweet_id, limit)

    async def fetch_reposters(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        return await self._fetch_accounts("retweeters", tweet_id, limit)

    async def _fetch_accounts(
        self, command: str, tweet_id: str, limit: int
    ) -> list[dict[str, Any]]:
        args = [command, str(tweet_id)]
        if limit >= 0:
            args.extend(["-n", str(limit)])
        rows = await self._run(args)
        accounts: list[dict[str, Any]] = []
        for row in rows:
            user = row.get("user") or row
            account_id = str(user.get("id") or user.get("account_id") or "")
            if not account_id:
                continue
            accounts.append(
                {
                    "account_id": account_id,
                    "username": str(user.get("username") or user.get("screen_name") or ""),
                    "source_tool": self.name,
                    "auth_tier": self.auth_tier,
                    "raw_evidence_reference": f"x-cli:{command}:{tweet_id}",
                }
            )
            if limit >= 0 and len(accounts) >= limit:
                break
        return accounts


def _run_probe(args: list[str], timeout: float = 3.0) -> str:
    """Run a read-only version/help probe; never raises."""
    import subprocess

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv list, never a shell
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    output = (completed.stdout or b"") + (completed.stderr or b"")
    return output.decode("utf-8", errors="replace").strip()[:2000]


def _parse_json_lines(payload: str | bytes, *, adapter: str) -> list[dict[str, Any]]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    text = payload.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            for key in ("results", "data", "items", "tweets"):
                if isinstance(parsed.get(key), list):
                    return [item for item in parsed[key] if isinstance(item, dict)]
            return [parsed]
    except json.JSONDecodeError:
        pass
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CollectionError(
                f"x-cli returned invalid JSONL: {type(exc).__name__}", adapter=adapter
            ) from exc
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _map_tweet(row: dict[str, Any], tweet_id: str, tool: str, tier: str) -> dict[str, Any]:
    author = row.get("author") or row.get("user") or {}
    metrics = {
        "likes": _int(row.get("likes") or row.get("like_count") or 0),
        "reposts": _int(row.get("retweets") or row.get("retweet_count") or 0),
        "replies": _int(row.get("replies") or row.get("reply_count") or 0),
        "quotes": _int(row.get("quotes") or row.get("quote_count") or 0),
    }
    for key, aliases in (
        ("views", ("views", "view_count")),
        ("bookmarks", ("bookmarks", "bookmark_count")),
    ):
        value = next((row[alias] for alias in aliases if row.get(alias) is not None), None)
        if value is not None:
            metrics[key] = _int(value)
    return {
        "post_id": str(row.get("id") or tweet_id),
        "url": str(row.get("url") or f"https://x.com/i/web/status/{tweet_id}"),
        "text": str(row.get("text") or row.get("full_text") or row.get("content") or ""),
        "created_at": _iso(row.get("created_at") or row.get("date")),
        "author_id": str(author.get("id") or row.get("author_id") or ""),
        "author_username": str(
            author.get("username") or author.get("screen_name") or row.get("username") or ""
        ),
        "metrics": metrics,
        "source_tool": tool,
        "auth_tier": tier,
        "raw_evidence_reference": "x-cli:tweet",
    }


def _iso(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat(timespec="seconds")
    try:
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            .astimezone(UTC)
            .isoformat(timespec="seconds")
        )
    except ValueError:
        return ""


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
