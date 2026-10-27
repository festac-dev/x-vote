"""Scweet adapter (Tier 2, auth_token, opt-in).

Scweet is intentionally disabled by default. Its public API has changed across
releases, so this adapter supports the documented reply/repost helpers when the
installed version exposes them and reports a clear limitation otherwise. It does
not attempt poll collection.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime
from typing import Any

from ..config import ScweetSettings
from .base import (
    AcquisitionAdapter,
    CollectionError,
    CredentialsMissing,
    NormalizedPoll,
    NormalizedReply,
    SourceUnavailableError,
)


class ScweetAdapter(AcquisitionAdapter):
    name = "Scweet"
    auth_tier = "auth_token"
    license_note = (
        "Wrap according to the Scweet project license and X's terms; authorized sessions only. "
        "Scweet does not document a poll method, so poll data is never invented."
    )

    def __init__(self, settings: ScweetSettings, *, client: Any = None) -> None:
        self.settings = settings
        self._client = client

    def capabilities(self) -> dict[str, bool]:
        return {
            "poll": False,
            "replies": True,
            "individual_likers": False,
            "individual_reposters": True,
            "auth_required": True,
        }

    def version_info(self) -> str:
        try:
            from importlib import metadata

            return metadata.version("Scweet")
        except Exception:
            return ""

    def command_probe(self) -> dict[str, Any]:
        try:
            import Scweet  # type: ignore
        except Exception:
            return {"probe": "Scweet could not be imported"}
        return {
            "module": True,
            "get_replies": callable(getattr(Scweet, "get_replies", None))
            or callable(getattr(Scweet, "replies", None)),
            "get_reposters": callable(getattr(Scweet, "get_reposters", None))
            or callable(getattr(Scweet, "reposters", None)),
            "poll_method_documented": False,
        }

    def available(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "adapter is opt-in and disabled"
        if self._client is not None:
            return True, ""
        if importlib.util.find_spec("Scweet") is None:
            return False, "Scweet is not installed"
        if not os.environ.get(self.settings.auth_token_env, "").strip():
            return False, "Scweet auth token is not configured"
        return True, ""

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import Scweet  # type: ignore

            token = os.environ.get(self.settings.auth_token_env, "").strip()
            if hasattr(Scweet, "Scweet"):
                self._client = Scweet.Scweet(auth_token=token)
            elif hasattr(Scweet, "scweet"):
                self._client = Scweet.scweet
            else:
                self._client = Scweet
        except Exception as exc:
            raise CredentialsMissing(
                f"Scweet could not start: {type(exc).__name__}", adapter=self.name
            ) from exc
        return self._client

    async def fetch_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        return None

    async def fetch_poll(self, tweet_id: str) -> NormalizedPoll | None:
        return None

    async def fetch_replies(self, tweet_id: str, limit: int = -1) -> list[NormalizedReply]:
        client = self._get_client()
        func = getattr(client, "get_replies", None) or getattr(client, "replies", None)
        if func is None:
            raise SourceUnavailableError(
                "installed Scweet version exposes no reply method", adapter=self.name
            )
        try:
            rows = func(tweet_id)
            if hasattr(rows, "__await__"):
                rows = await rows
            rows = list(rows or [])
        except Exception as exc:
            raise CollectionError(
                f"Scweet reply fetch failed: {type(exc).__name__}", adapter=self.name
            ) from exc
        replies: list[NormalizedReply] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            reply_id = str(row.get("id") or row.get("tweet_id") or "")
            if not reply_id:
                continue
            replies.append(
                NormalizedReply(
                    reply_id=reply_id,
                    author_id=str(row.get("user_id") or row.get("author_id") or ""),
                    author_username=str(
                        row.get("username") or row.get("user_screen_name") or ""
                    ).lstrip("@"),
                    text=str(row.get("text") or row.get("full_text") or ""),
                    created_at=_iso(row.get("created_at") or row.get("timestamp")),
                    url=str(row.get("url") or f"https://x.com/i/web/status/{reply_id}"),
                    parent_post_id=str(tweet_id),
                    source_tool=self.name,
                    auth_tier=self.auth_tier,
                    raw_evidence_reference=f"scweet:replies:{tweet_id}",
                )
            )
            if limit >= 0 and len(replies) >= limit:
                break
        return replies

    async def fetch_likers(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        raise CollectionError(
            "Scweet individual likers are not documented; use twscrape or x-cli",
            adapter=self.name,
        )

    async def fetch_reposters(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        client = self._get_client()
        func = getattr(client, "get_reposts", None) or getattr(client, "get_reposters", None)
        if func is None:
            raise SourceUnavailableError(
                "installed Scweet version exposes no repost method", adapter=self.name
            )
        try:
            rows = func(tweet_id)
            if hasattr(rows, "__await__"):
                rows = await rows
            rows = list(rows or [])
        except Exception as exc:
            raise CollectionError(
                f"Scweet repost fetch failed: {type(exc).__name__}", adapter=self.name
            ) from exc
        accounts: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            account_id = str(row.get("user_id") or row.get("id") or "")
            if not account_id:
                continue
            accounts.append(
                {
                    "account_id": account_id,
                    "username": str(row.get("username") or row.get("user_screen_name") or ""),
                    "source_tool": self.name,
                    "auth_tier": self.auth_tier,
                    "raw_evidence_reference": f"scweet:reposts:{tweet_id}",
                }
            )
            if limit >= 0 and len(accounts) >= limit:
                break
        return accounts


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
