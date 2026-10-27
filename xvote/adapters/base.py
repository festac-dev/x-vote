"""Acquisition adapter contract shared by every open-source source."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

AUTH_TIERS = {"none", "guest", "auth_token", "session"}


class AdapterError(RuntimeError):
    code = "adapter_error"

    def __init__(self, message: str, *, adapter: str = "") -> None:
        super().__init__(message)
        self.adapter = adapter


class CollectionError(AdapterError):
    code = "collection_error"


class RateLimitError(AdapterError):
    code = "rate_limited"


class PermissionRestrictedError(AdapterError):
    code = "permission_restricted"


class SourceUnavailableError(AdapterError):
    code = "source_unavailable"


class AdapterUnavailable(AdapterError):
    code = "adapter_unavailable"


class CredentialsMissing(AdapterUnavailable):
    code = "credentials_missing"


class CapabilityUnsupported(AdapterError):
    code = "capability_unsupported"


@dataclass
class NormalizedReply:
    reply_id: str
    author_id: str
    author_username: str
    text: str
    created_at: str
    url: str
    parent_post_id: str
    is_quote: bool = False
    quoted_post_id: str = ""
    source_tool: str = ""
    auth_tier: str = "none"
    raw_evidence_reference: str = ""
    metrics: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class NormalizedPoll:
    post_id: str
    options: list[dict[str, Any]]
    total_votes: int
    finished: bool
    collected_at: str
    source_tool: str
    auth_tier: str
    raw_evidence_reference: str = ""
    url: str = ""
    exact_counts: bool = True
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def normalized_account(
    account_id: str,
    username: str = "",
    *,
    source_tool: str = "",
    auth_tier: str = "none",
    raw_evidence_reference: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "account_id": str(account_id),
        "username": (username or "").lstrip("@"),
        "source_tool": source_tool,
        "auth_tier": auth_tier,
        "raw_evidence_reference": raw_evidence_reference,
    }
    if extra:
        payload.update(extra)
    return payload


COMPLIANCE_NOTE = (
    "Use authorized sessions only. Respect the tool license, X's applicable terms and access "
    "controls, and any self-hosted instance policies. Never bypass CAPTCHA, authentication "
    "restrictions or rate limits. An open-source library does not by itself make every "
    "acquisition method permitted."
)
CAPABILITY_SOURCE_NOTE = (
    "Capabilities are declared by this adapter. Installed tool versions and their commands are "
    "probed at runtime; unverified commands are reported as unknown, never assumed to work."
)


class AcquisitionAdapter(ABC):
    name: str = "base"
    auth_tier: str = "none"
    license_note: str = ""

    @abstractmethod
    async def fetch_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        """Fetch a single post, or None if unavailable."""

    @abstractmethod
    async def fetch_replies(self, tweet_id: str, limit: int = -1) -> list[NormalizedReply]:
        """Fetch replies. An empty list is not proof that no replies exist."""

    @abstractmethod
    async def fetch_poll(self, tweet_id: str) -> NormalizedPoll | None:
        """Fetch aggregate poll results. Never synthesize individual votes."""

    @abstractmethod
    async def fetch_likers(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        """Fetch individually observed liker accounts, where supported."""

    @abstractmethod
    async def fetch_reposters(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        """Fetch individually observed reposter accounts, where supported."""

    @abstractmethod
    def capabilities(self) -> dict[str, bool]:
        """Return poll/replies/individual_likers/individual_reposters/auth_required."""

    def available(self) -> tuple[bool, str]:
        return True, ""

    def version_info(self) -> str:
        """Detected version of the underlying tool, or empty when unknown."""
        return ""

    def command_probe(self) -> dict[str, Any]:
        """Optional probe of the installed tool's supported commands.

        Values are True (confirmed), False (confirmed missing) or None (unknown).
        """
        return {}

    def probe(self) -> dict[str, Any]:
        available, reason = self.available()
        return {
            "name": self.name,
            "auth_tier": self.auth_tier,
            "available": available,
            "unavailable_reason": reason,
            "version": self.version_info(),
            "capabilities": self.capabilities(),
            "commands": self.command_probe(),
            "license_note": self.license_note,
            "compliance_note": COMPLIANCE_NOTE,
            "capability_source": CAPABILITY_SOURCE_NOTE,
        }

    def describe(self) -> dict[str, Any]:
        return self.probe()
