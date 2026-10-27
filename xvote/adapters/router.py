"""Single acquisition entry point with operator-ordered fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..models import CollectionStatus
from .base import (
    AcquisitionAdapter,
    AdapterError,
    CapabilityUnsupported,
    CollectionError,
    CredentialsMissing,
    PermissionRestrictedError,
    RateLimitError,
    SourceUnavailableError,
)

CAPABILITY_KEYS = {
    "tweet": None,
    "replies": "replies",
    "poll": "poll",
    "likers": "individual_likers",
    "reposters": "individual_reposters",
}


@dataclass
class AdapterAttempt:
    adapter: str
    method: str
    auth_tier: str = ""
    status: str = "skipped"
    error_code: str = ""
    message: str = ""
    result_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RouterResult:
    operation: str
    value: Any
    source_tool: str
    auth_tier: str
    collection_status: str
    attempts: list[AdapterAttempt] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "source_tool": self.source_tool,
            "auth_tier": self.auth_tier,
            "collection_status": self.collection_status,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "limitations": list(self.limitations),
            "partial": self.partial,
        }


class AcquisitionRouter:
    def __init__(
        self,
        adapters: dict[str, AcquisitionAdapter],
        preference_order: list[str],
        *,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.adapters = adapters
        ordered: list[str] = []
        for name in preference_order:
            if name in adapters and name not in ordered:
                ordered.append(name)
        for name in adapters:
            if name not in ordered:
                ordered.append(name)
        self.preference_order = ordered
        self.timeout_seconds = timeout_seconds

    def capability_matrix(self) -> list[dict[str, Any]]:
        return [
            adapter.describe()
            for name in self.preference_order
            for adapter in [self.adapters[name]]
        ]

    def order(self) -> list[str]:
        return list(self.preference_order)

    def _candidates(
        self, operation: str, attempts: list[AdapterAttempt]
    ) -> list[tuple[str, AcquisitionAdapter]]:
        capability = CAPABILITY_KEYS.get(operation)
        candidates: list[tuple[str, AcquisitionAdapter]] = []
        for name in self.preference_order:
            adapter = self.adapters[name]
            if capability is None:
                pass
            else:
                caps = adapter.capabilities()
                if not caps.get(capability, False):
                    attempts.append(
                        AdapterAttempt(
                            adapter=name,
                            method=operation,
                            auth_tier=adapter.auth_tier,
                            status="skipped",
                            error_code=CapabilityUnsupported.code,
                            message=f"adapter does not support {operation}",
                        )
                    )
                    continue
            available, reason = adapter.available()
            if not available:
                attempts.append(
                    AdapterAttempt(
                        adapter=name,
                        method=operation,
                        auth_tier=adapter.auth_tier,
                        status="skipped",
                        error_code=CredentialsMissing.code
                        if "credential" in reason.lower()
                        or "account" in reason.lower()
                        or "cookie" in reason.lower()
                        else "adapter_unavailable",
                        message=reason,
                    )
                )
                continue
            candidates.append((name, adapter))
        return candidates

    async def _run(
        self,
        operation: str,
        invoke: Callable[[AcquisitionAdapter], Awaitable[Any]],
        *,
        empty: Callable[[Any], bool],
        count: Callable[[Any], int],
    ) -> RouterResult:
        attempts: list[AdapterAttempt] = []
        failures = 0
        candidates = self._candidates(operation, attempts)
        if not candidates:
            detail = "; ".join(
                f"{attempt.adapter}: {attempt.message}"
                for attempt in attempts
                if attempt.status == "skipped"
            )
            raise CollectionError(
                f"no adapter available for {operation}" + (f" ({detail})" if detail else "")
            )
        for index, (name, adapter) in enumerate(candidates):
            attempt = AdapterAttempt(adapter=name, method=operation, auth_tier=adapter.auth_tier)
            try:
                value = await asyncio.wait_for(invoke(adapter), timeout=self.timeout_seconds)
                if value is None or empty(value):
                    attempt.status = "empty"
                    attempt.error_code = "no_data"
                    attempt.message = "adapter returned no data"
                    attempts.append(attempt)
                    failures += 1
                    continue
                attempt.status = "success"
                attempt.result_count = count(value)
                attempts.append(attempt)
                partial = failures > 0 or index > 0
                status = (
                    CollectionStatus.PARTIALLY_COMPLETE.value
                    if partial
                    else CollectionStatus.COMPLETE_FOR_SCOPE.value
                )
                limitations: list[str] = []
                if partial:
                    limitations.append(
                        "A higher-preference adapter was unavailable or returned no data; "
                        f"{name} supplied the result."
                    )
                if operation == "poll":
                    limitations.append(
                        "Native poll results are anonymous aggregates; individual voter "
                        "verification is unavailable."
                    )
                return RouterResult(
                    operation=operation,
                    value=value,
                    source_tool=name,
                    auth_tier=adapter.auth_tier,
                    collection_status=status,
                    attempts=attempts,
                    limitations=limitations,
                    partial=partial,
                )
            except TimeoutError:
                attempt.status = "failed"
                attempt.error_code = "timeout"
                attempt.message = "adapter timed out"
            except RateLimitError as exc:
                attempt.status = "failed"
                attempt.error_code = exc.code
                attempt.message = str(exc) or "rate limited"
            except PermissionRestrictedError as exc:
                attempt.status = "failed"
                attempt.error_code = exc.code
                attempt.message = str(exc) or "permission restricted"
            except CredentialsMissing as exc:
                attempt.status = "failed"
                attempt.error_code = exc.code
                attempt.message = str(exc) or "credentials unavailable"
            except SourceUnavailableError as exc:
                attempt.status = "failed"
                attempt.error_code = exc.code
                attempt.message = str(exc) or "source unavailable"
            except CapabilityUnsupported as exc:
                attempt.status = "failed"
                attempt.error_code = exc.code
                attempt.message = str(exc) or "capability unsupported"
            except AdapterError as exc:
                attempt.status = "failed"
                attempt.error_code = exc.code
                attempt.message = str(exc) or "adapter failed"
            except Exception as exc:  # isolate adapter defects from the workflow
                attempt.status = "failed"
                attempt.error_code = "adapter_error"
                attempt.message = f"unexpected adapter error: {type(exc).__name__}"
            attempts.append(attempt)
            failures += 1

        statuses = {attempt.error_code for attempt in attempts if attempt.status == "failed"}
        if "rate_limited" in statuses:
            status = CollectionStatus.RATE_LIMITED.value
        elif "permission_restricted" in statuses:
            status = CollectionStatus.PERMISSION_RESTRICTED.value
        elif statuses and statuses <= {"credentials_missing"}:
            status = CollectionStatus.SOURCE_UNAVAILABLE.value
        elif statuses:
            status = CollectionStatus.PARTIALLY_COMPLETE.value
        else:
            status = CollectionStatus.SOURCE_UNAVAILABLE.value
        failure_summary = ", ".join(
            f"{attempt.adapter}={attempt.error_code or attempt.status}"
            for attempt in attempts
            if attempt.status in {"failed", "empty"}
        )
        raise CollectionError(
            f"{operation} could not be collected ({failure_summary or 'no usable adapter'})"
        )

    async def fetch_tweet(self, tweet_id: str) -> RouterResult:
        return await self._run(
            "tweet",
            lambda adapter: adapter.fetch_tweet(tweet_id),
            empty=lambda value: not value,
            count=lambda value: 1,
        )

    async def fetch_replies(self, tweet_id: str, limit: int = -1) -> RouterResult:
        return await self._run(
            "replies",
            lambda adapter: adapter.fetch_replies(tweet_id, limit),
            empty=lambda value: len(value) == 0,
            count=len,
        )

    async def fetch_poll(self, tweet_id: str) -> RouterResult:
        return await self._run(
            "poll",
            lambda adapter: adapter.fetch_poll(tweet_id),
            empty=lambda value: value is None,
            count=lambda value: len(value.options) if value else 0,
        )

    async def fetch_likers(self, tweet_id: str, limit: int = -1) -> RouterResult:
        return await self._run(
            "likers",
            lambda adapter: adapter.fetch_likers(tweet_id, limit),
            empty=lambda value: len(value) == 0,
            count=len,
        )

    async def fetch_reposters(self, tweet_id: str, limit: int = -1) -> RouterResult:
        return await self._run(
            "reposters",
            lambda adapter: adapter.fetch_reposters(tweet_id, limit),
            empty=lambda value: len(value) == 0,
            count=len,
        )


def status_from_failures(attempts: list[AdapterAttempt], fallback: str) -> str:
    codes = {a.error_code for a in attempts if a.status == "failed"}
    if "rate_limited" in codes:
        return CollectionStatus.RATE_LIMITED.value
    if "permission_restricted" in codes:
        return CollectionStatus.PERMISSION_RESTRICTED.value
    if "source_unavailable" in codes:
        return CollectionStatus.SOURCE_UNAVAILABLE.value
    return fallback
