"""x-tweet-fetcher compatible adapter: FxTwitter + self-hosted Nitter.

The open-source x-tweet-fetcher projects use FxTwitter for single posts and a
Nitter instance for timelines/search. This adapter implements that read-only
approach directly, so it does not require installing another Python package.
Public Nitter instances are not used: the operator must point it at their own.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from ..config import XTweetFetcherSettings
from ..urls import OutboundFetchError, fetch_json, fetch_text, nitter_host
from .base import (
    AcquisitionAdapter,
    CollectionError,
    NormalizedPoll,
    NormalizedReply,
    SourceUnavailableError,
)

FX_HOSTS = {"api.fxtwitter.com"}

_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _NitterParser(HTMLParser):
    """Best-effort parser for Nitter status pages.

    Nitter markup is not a stable API. If a page cannot be parsed the adapter
    reports failure rather than claiming a complete reply set.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._stack: list[str] = []
        self._item: dict[str, Any] | None = None
        self._item_depth = -1
        self._main: dict[str, Any] | None = None
        self._main_depth = -1
        self._capture = ""
        self._capture_depth = -1

    def _active(self) -> dict[str, Any] | None:
        return self._item if self._item is not None else self._main

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        depth = len(self._stack)
        if tag not in _VOID_TAGS:
            self._stack.append(tag)

        if "timeline-item" in classes and self._item is None:
            self._item = {
                "reply_id": "",
                "author_username": "",
                "text": "",
                "created_at": "",
                "url": "",
                "is_quote": "quote" in classes,
            }
            self._item_depth = depth
        elif "main-tweet" in classes and self._main is None and self._item is None:
            self._main = {
                "reply_id": "",
                "author_username": "",
                "text": "",
                "created_at": "",
                "url": "",
                "is_quote": False,
            }
            self._main_depth = depth

        active = self._active()
        if active is None:
            return
        if "tweet-link" in classes:
            href = attrs_dict.get("href", "")
            match = re.search(r"/status(?:es)?/(\d+)", href)
            if match:
                active["reply_id"] = match.group(1)
            if href.startswith("/"):
                active["url"] = href
        elif "tweet-content" in classes:
            self._capture = "text"
            self._capture_depth = depth
        elif "username" in classes:
            self._capture = "username"
            self._capture_depth = depth
        elif "tweet-date" in classes:
            self._capture = "date"
            self._capture_depth = depth
        if tag == "a" and self._capture == "date":
            title = attrs_dict.get("title")
            if title:
                active["created_at"] = _parse_nitter_date(title)
            href = attrs_dict.get("href", "")
            match = re.search(r"/status(?:es)?/(\d+)", href)
            if match and not active.get("reply_id"):
                active["reply_id"] = match.group(1)

    def handle_endtag(self, tag: str) -> None:
        closing_depth = len(self._stack) - 1
        if (
            self._capture
            and closing_depth == self._capture_depth
            and tag in {"div", "span", "p", "a"}
        ):
            self._capture = ""
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
        else:
            # Tolerate minor markup imbalance without growing the stack unbounded.
            for index in range(len(self._stack) - 1, -1, -1):
                if self._stack[index] == tag:
                    del self._stack[index:]
                    break
        if self._item is not None and closing_depth == self._item_depth:
            if self._item.get("reply_id") or str(self._item.get("text") or "").strip():
                self.items.append(self._item)
            self._item = None
            self._item_depth = -1
        elif self._main is not None and closing_depth == self._main_depth:
            if str(self._main.get("text") or "").strip() or self._main.get("author_username"):
                self.items.insert(0, self._main)
            self._main = None
            self._main_depth = -1

    def handle_data(self, data: str) -> None:
        active = self._active()
        if active is None:
            return
        if self._capture == "text":
            active["text"] += data
        elif self._capture == "username":
            active["author_username"] += data


def _parse_nitter_date(raw: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(raw).strip().replace("·", " "))
    candidates = [
        "%b %d, %Y %I:%M %p %Z",
        "%b %d, %Y %H:%M %Z",
        "%d %b %Y %H:%M %Z",
        "%b %d, %Y",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in candidates:
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""


class XTweetFetcherAdapter(AcquisitionAdapter):
    name = "x-tweet-fetcher"
    auth_tier = "none"
    license_note = (
        "Uses FxTwitter only for single posts and an operator-hosted Nitter instance for "
        "replies. Verify the licenses and instance policies of both services; public Nitter "
        "instances are not relied upon and no access controls may be bypassed."
    )

    def __init__(self, settings: XTweetFetcherSettings) -> None:
        self.settings = settings
        self.nitter_base = settings.nitter_base.rstrip("/")
        try:
            self._nitter_host = nitter_host(self.nitter_base)
        except ValueError:
            self._nitter_host = ""

    def capabilities(self) -> dict[str, bool]:
        return {
            "poll": False,
            "replies": True,
            "individual_likers": False,
            "individual_reposters": False,
            "auth_required": False,
        }

    def version_info(self) -> str:
        try:
            from importlib import metadata

            return metadata.version("x-tweet-fetcher")
        except Exception:
            return ""

    def command_probe(self) -> dict[str, Any]:
        return {
            "fxtwitter_single_posts": "configured",
            "nitter_base_configured": bool(self.nitter_base),
            "nitter_base_valid": bool(self._nitter_host),
            "self_hosted_nitter_required": True,
            "poll_supported": False,
        }

    def available(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "disabled in acquisition configuration"
        if not self._nitter_host:
            return False, "nitter_base is invalid; replies are unavailable"
        return True, ""

    async def _get_json(self, url: str, hosts: set[str]) -> Any:
        try:
            return await asyncio.to_thread(
                fetch_json,
                url,
                hosts,
                timeout=self.settings.request_timeout_seconds,
                max_bytes=self.settings.max_response_bytes,
            )
        except OutboundFetchError as exc:
            raise SourceUnavailableError(str(exc), adapter=self.name) from exc

    async def _get_text(self, url: str, hosts: set[str]) -> str:
        try:
            return await asyncio.to_thread(
                fetch_text,
                url,
                hosts,
                timeout=self.settings.request_timeout_seconds,
                max_bytes=self.settings.max_response_bytes,
            )
        except OutboundFetchError as exc:
            raise SourceUnavailableError(str(exc), adapter=self.name) from exc

    async def fetch_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        payload = await self._get_json(
            f"https://api.fxtwitter.com/status/{quote(tweet_id)}", FX_HOSTS
        )
        tweet = payload.get("tweet") if isinstance(payload, dict) else None
        if not isinstance(tweet, dict):
            raise CollectionError("FxTwitter returned no tweet object", adapter=self.name)
        author = tweet.get("author") or {}
        return {
            "post_id": str(tweet.get("id") or tweet_id),
            "url": str(tweet.get("url") or f"https://x.com/i/web/status/{tweet_id}"),
            "text": str(tweet.get("text") or ""),
            "created_at": _normalize_timestamp(tweet.get("created_at")),
            "author_id": str(author.get("id") or ""),
            "author_username": str(author.get("screen_name") or ""),
            "metrics": {
                "likes": _int(tweet.get("likes")),
                "reposts": _int(tweet.get("retweets")),
                "replies": _int(tweet.get("replies")),
                "quotes": _int(tweet.get("quotes")),
            },
            "source_tool": self.name,
            "auth_tier": self.auth_tier,
            "raw_evidence_reference": "fxtwitter:status",
        }

    async def fetch_replies(self, tweet_id: str, limit: int = -1) -> list[NormalizedReply]:
        if not self.settings.enabled:
            raise SourceUnavailableError("adapter disabled", adapter=self.name)
        if not self._nitter_host:
            raise SourceUnavailableError(
                "self-hosted Nitter is not configured; replies are unavailable",
                adapter=self.name,
            )
        tweet = await self.fetch_tweet(tweet_id)
        username = (tweet or {}).get("author_username") or ""
        if username:
            url = f"{self.nitter_base}/{quote(username)}/status/{quote(tweet_id)}"
        else:
            url = f"{self.nitter_base}/i/web/status/{quote(tweet_id)}"
        page = await self._get_text(url, {self._nitter_host})
        parser = _NitterParser()
        try:
            parser.feed(page)
        except Exception as exc:
            raise CollectionError(
                f"Nitter markup could not be parsed: {type(exc).__name__}", adapter=self.name
            ) from exc
        replies: list[NormalizedReply] = []
        seen: set[str] = set()
        for item in parser.items:
            reply_id = str(item.get("reply_id") or "")
            text = str(item.get("text") or "").strip()
            if not reply_id or reply_id == tweet_id or reply_id in seen or not text:
                continue
            seen.add(reply_id)
            author = str(item.get("author_username") or "").lstrip("@")
            href = str(item.get("url") or "")
            full_url = (
                href
                if href.startswith("http")
                else f"https://x.com/{author or 'i'}/status/{reply_id}"
            )
            replies.append(
                NormalizedReply(
                    reply_id=reply_id,
                    author_id="",
                    author_username=author,
                    text=text,
                    created_at=str(item.get("created_at") or ""),
                    url=full_url,
                    parent_post_id=tweet_id,
                    is_quote=bool(item.get("is_quote")),
                    source_tool=self.name,
                    auth_tier=self.auth_tier,
                    raw_evidence_reference=f"nitter:status:{tweet_id}",
                )
            )
            if limit >= 0 and len(replies) >= limit:
                break
        if not replies:
            raise CollectionError(
                "Nitter returned no parseable replies; coverage is unknown, not zero",
                adapter=self.name,
            )
        return replies

    async def fetch_poll(self, tweet_id: str) -> NormalizedPoll | None:
        return None

    async def fetch_likers(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        raise CollectionError(
            "x-tweet-fetcher does not expose individual likers", adapter=self.name
        )

    async def fetch_reposters(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        raise CollectionError(
            "x-tweet-fetcher does not expose individual reposters", adapter=self.name
        )


def _normalize_timestamp(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat(timespec="seconds")
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
