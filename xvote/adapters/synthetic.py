"""Explicitly gated synthetic acquisition adapter (tests and labelled demo mode)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .base import (
    AcquisitionAdapter,
    CollectionError,
    NormalizedPoll,
    NormalizedReply,
    SourceUnavailableError,
)

FIXTURE_TOOL = "synthetic-fixture"


class SyntheticFixtureAdapter(AcquisitionAdapter):
    name = "synthetic-fixture"
    auth_tier = "none"

    def __init__(self, fixture: dict[str, Any], *, enabled: bool = False) -> None:
        self.fixture = fixture
        self._enabled = enabled

    def available(self) -> tuple[bool, str]:
        if not self._enabled:
            return False, "synthetic fixtures are not enabled for this session"
        return True, ""

    def capabilities(self) -> dict[str, bool]:
        return {
            "poll": True,
            "replies": True,
            "individual_likers": True,
            "individual_reposters": True,
            "auth_required": False,
        }

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise SourceUnavailableError("synthetic fixtures are not enabled", adapter=self.name)

    async def fetch_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        self._require_enabled()
        post = (self.fixture.get("posts") or {}).get(str(tweet_id))
        if not post:
            return None
        return {
            **post,
            "source_tool": FIXTURE_TOOL,
            "auth_tier": self.auth_tier,
            "raw_evidence_reference": f"synthetic:post:{tweet_id}",
            "is_test_data": True,
        }

    async def fetch_replies(self, tweet_id: str, limit: int = -1) -> list[NormalizedReply]:
        self._require_enabled()
        rows = (self.fixture.get("replies") or {}).get(str(tweet_id), [])
        replies = []
        for row in rows:
            replies.append(
                NormalizedReply(
                    reply_id=str(row["reply_id"]),
                    author_id=str(row.get("author_id") or ""),
                    author_username=str(row.get("author_username") or ""),
                    text=str(row.get("text") or ""),
                    created_at=str(row.get("created_at") or ""),
                    url=str(row.get("url") or ""),
                    parent_post_id=str(tweet_id),
                    source_tool=str(row.get("source_tool") or FIXTURE_TOOL),
                    auth_tier=self.auth_tier,
                    raw_evidence_reference=f"synthetic:reply:{row['reply_id']}",
                )
            )
            if limit >= 0 and len(replies) >= limit:
                break
        return replies

    async def fetch_poll(self, tweet_id: str) -> NormalizedPoll | None:
        self._require_enabled()
        poll = (self.fixture.get("polls") or {}).get(str(tweet_id))
        if not poll:
            return None
        return NormalizedPoll(
            post_id=str(tweet_id),
            options=[dict(option) for option in poll.get("options", [])],
            total_votes=sum(int(option.get("votes", 0)) for option in poll.get("options", [])),
            finished=bool(poll.get("finished", False)),
            collected_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source_tool=FIXTURE_TOOL,
            auth_tier=self.auth_tier,
            raw_evidence_reference=f"synthetic:poll:{tweet_id}",
            url=str(poll.get("url") or ""),
            exact_counts=bool(poll.get("exact_counts", True)),
            limitations=list(poll.get("limitations") or []),
        )

    async def fetch_likers(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        self._require_enabled()
        return self._accounts("likers", tweet_id, limit)

    async def fetch_reposters(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        self._require_enabled()
        return self._accounts("reposters", tweet_id, limit)

    def _accounts(self, kind: str, tweet_id: str, limit: int) -> list[dict[str, Any]]:
        rows = (self.fixture.get(kind) or {}).get(str(tweet_id), [])
        post = (self.fixture.get("posts") or {}).get(str(tweet_id), {})
        post_url = str(post.get("url") or f"https://x.com/i/web/status/{tweet_id}")
        accounts = []
        for row in rows:
            accounts.append(
                {
                    "account_id": str(row.get("account_id") or ""),
                    "username": str(row.get("username") or ""),
                    "source_url": post_url,
                    "source_tool": FIXTURE_TOOL,
                    "auth_tier": self.auth_tier,
                    "raw_evidence_reference": (
                        f"synthetic:{kind}:{tweet_id}:{row.get('account_id')}"
                    ),
                }
            )
            if limit >= 0 and len(accounts) >= limit:
                break
        return accounts


def ensure_fixture_shape(fixture: dict[str, Any]) -> None:
    if not isinstance(fixture, dict):
        raise CollectionError("fixture must be an object")
    for key in ("posts", "replies", "polls", "likers", "reposters"):
        fixture.setdefault(key, {})
