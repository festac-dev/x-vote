from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from xvote.adapters.base import (
    AcquisitionAdapter,
    CollectionError,
    CredentialsMissing,
    RateLimitError,
)
from xvote.adapters.manual import (
    ImportValidationError,
    ManualImportAdapter,
    OCREvidenceAdapter,
)
from xvote.adapters.router import AcquisitionRouter


def make_reply(reply_id: str = "901") -> dict[str, str]:
    return {
        "reply_id": reply_id,
        "author_id": "42",
        "author_username": "someone",
        "text": "Vote for Alpha",
        "created_at": "2026-01-01T00:00:00Z",
        "url": "https://x.com/someone/status/" + reply_id,
        "parent_post_id": "900",
    }


class FakeAdapter(AcquisitionAdapter):
    name = "fake"
    auth_tier = "none"

    def __init__(
        self,
        *,
        name: str = "fake",
        replies: list[dict[str, str]] | None = None,
        poll: dict[str, object] | None = None,
        capabilities: dict[str, bool] | None = None,
        available: tuple[bool, str] = (True, ""),
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self._replies = replies or []
        self._poll = poll
        self._capabilities = capabilities or {
            "poll": True,
            "replies": True,
            "individual_likers": True,
            "individual_reposters": True,
            "auth_required": False,
        }
        self._available = available
        self._error = error
        self._delay = delay

    def capabilities(self) -> dict[str, bool]:
        return dict(self._capabilities)

    def available(self) -> tuple[bool, str]:
        return self._available

    async def fetch_tweet(self, tweet_id: str):
        return {"id": tweet_id} if not self._error else self._raise()

    async def fetch_replies(self, tweet_id: str, limit: int = -1):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            self._raise()
        return list(self._replies)

    async def fetch_poll(self, tweet_id: str):
        return self._poll

    async def fetch_likers(self, tweet_id: str, limit: int = -1):
        return []

    async def fetch_reposters(self, tweet_id: str, limit: int = -1):
        return []

    def _raise(self):
        raise self._error  # type: ignore[misc]


def test_router_falls_through_rate_limit_to_next_adapter() -> None:
    limited = FakeAdapter(name="limited", error=RateLimitError("rate limited"))
    fallback = FakeAdapter(name="fallback", replies=[make_reply()])
    router = AcquisitionRouter({"limited": limited, "fallback": fallback}, ["limited", "fallback"])
    result = asyncio.run(router.fetch_replies("900"))
    assert result.source_tool == "fallback"
    assert result.partial is True
    assert result.value
    assert result.attempts[0].error_code == "rate_limited"
    assert any("higher-preference" in item for item in result.limitations)


def test_router_skips_adapters_missing_poll_capability() -> None:
    no_poll = FakeAdapter(name="no-poll", capabilities={"poll": False})
    poll = FakeAdapter(
        name="poll",
        poll=SimpleNamespace(
            post_id="900", options=[{"label": "Alpha", "votes": 5}], total_votes=5
        ),
    )
    router = AcquisitionRouter({"no-poll": no_poll, "poll": poll}, ["no-poll", "poll"])
    result = asyncio.run(router.fetch_poll("900"))
    assert result.source_tool == "poll"
    assert result.partial is False
    assert result.attempts[0].error_code == "capability_unsupported"
    assert any("anonymous aggregates" in item for item in result.limitations)


def test_router_never_fabricates_missing_poll_data() -> None:
    adapter = FakeAdapter(name="empty-poll", poll=None)
    router = AcquisitionRouter({"empty-poll": adapter}, ["empty-poll"])
    with pytest.raises(CollectionError) as excinfo:
        asyncio.run(router.fetch_poll("900"))
    assert "no_data" in str(excinfo.value)


def test_router_reports_credentials_missing() -> None:
    adapter = FakeAdapter(name="needs-login", available=(False, "credentials missing"))
    router = AcquisitionRouter({"needs-login": adapter}, ["needs-login"])
    with pytest.raises(CollectionError) as excinfo:
        asyncio.run(router.fetch_replies("900"))
    assert "credentials missing" in str(excinfo.value)


def test_router_times_out_and_uses_fallback() -> None:
    slow = FakeAdapter(name="slow", replies=[make_reply()], delay=0.2)
    fast = FakeAdapter(name="fast", replies=[make_reply("902")])
    router = AcquisitionRouter({"slow": slow, "fast": fast}, ["slow", "fast"], timeout_seconds=0.02)
    result = asyncio.run(router.fetch_replies("900"))
    assert result.source_tool == "fast"
    assert result.attempts[0].error_code == "timeout"


def test_router_isolates_unexpected_adapter_errors() -> None:
    broken = FakeAdapter(name="broken", error=CredentialsMissing("boom"))
    working = FakeAdapter(name="working", replies=[make_reply()])
    router = AcquisitionRouter({"broken": broken, "working": working}, ["broken", "working"])
    result = asyncio.run(router.fetch_replies("900"))
    assert result.source_tool == "working"
    assert result.attempts[0].status == "failed"


def test_manual_csv_and_json_import_validation() -> None:
    adapter = ManualImportAdapter()
    records = adapter.parse_csv("project,votes,method\nAlpha,10,manual_total\nBeta,3,\n")
    assert [record.project for record in records] == ["Alpha", "Beta"]
    assert records[0].votes == 10
    assert records[1].method == "manual_total"

    json_records = adapter.parse(
        '[{"project": "Alpha", "votes": 4, "account_id": "1", "source_url": "https://x.com/a/status/1"}]',
        format_hint="",
    )
    assert json_records[0].votes == 4
    assert json_records[0].method == "reply"

    status_records = adapter.parse_csv("project,votes,content_status\nAlpha,1,removed\n")
    assert status_records[0].content_status == "deleted"
    assert any("deleted" in item for item in status_records[0].limitations)
    with pytest.raises(ImportValidationError):
        adapter.parse_csv("project,votes,content_status\nAlpha,1,unknown\n")

    with pytest.raises(ImportValidationError):
        adapter.parse_csv("project,votes\nAlpha,many\n")
    with pytest.raises(ImportValidationError):
        adapter.parse_csv("votes\n3\n")
    with pytest.raises(ImportValidationError):
        adapter.parse_csv("")


def test_manual_import_rejects_invalid_confidence_and_keeps_limitations() -> None:
    adapter = ManualImportAdapter()
    with pytest.raises(ImportValidationError):
        adapter.parse_csv("project,votes,confidence\nAlpha,1,certain\n")
    records = adapter.parse_csv("project,votes\nAlpha,2\n", source_label="operator sheet")
    assert any("No source URL" in item for item in records[0].limitations)
    assert records[0].evidence_reference.startswith("manual:operator sheet:")


def test_ocr_text_parsing_and_image_validation() -> None:
    adapter = OCREvidenceAdapter()
    records = adapter.parse_text(
        "Alpha: 12 votes\nBeta - 4 votes\nnot a result\n", confidence="low"
    )
    assert [(record.project, record.votes) for record in records] == [
        ("Alpha", 12),
        ("Beta", 4),
    ]
    assert all(record.method == "ocr_total" for record in records)
    assert all("requires operator review" in record.note for record in records)
    with pytest.raises(ImportValidationError):
        adapter.parse_text("no parsable results")
    assert adapter.validate_image(b"\x89PNG\r\n\x1a\npayload") == "png"
    with pytest.raises(ImportValidationError):
        adapter.validate_image(b"GIF89a")
    with pytest.raises(ImportValidationError):
        adapter.validate_image(b"")
