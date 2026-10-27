"""twscrape adapter (Tier 2, auth_token).

twscrape is imported lazily. When it is not installed, or when no account pool is
configured, the adapter reports itself unavailable and the router falls through.
Credentials are read from the environment and loaded only into the session-scoped
account database inside the temporary session directory.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import TwscrapeSettings
from .base import (
    AcquisitionAdapter,
    CollectionError,
    CredentialsMissing,
    NormalizedPoll,
    NormalizedReply,
    RateLimitError,
    SourceUnavailableError,
)


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="seconds")
    if value:
        try:
            return (
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                .astimezone(UTC)
                .isoformat(timespec="seconds")
            )
        except ValueError:
            return ""
    return ""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _installed_version(distribution: str) -> str:
    try:
        from importlib import metadata

        return metadata.version(distribution)
    except Exception:
        return ""


class TwscrapeAdapter(AcquisitionAdapter):
    name = "twscrape"
    auth_tier = "auth_token"
    license_note = (
        "Wrap according to the twscrape project license and X's terms; authorized account "
        "pools only, no CAPTCHA or rate-limit bypass beyond the library's own rotation."
    )

    def version_info(self) -> str:
        return _installed_version("twscrape")

    def command_probe(self) -> dict[str, Any]:
        module = self._module
        if module is None:
            try:
                import importlib

                module = importlib.import_module("twscrape")
            except Exception:
                return {"probe": "twscrape could not be imported"}
        api = getattr(module, "API", None)
        pool = getattr(module, "AccountsPool", None)
        probed: dict[str, Any] = {
            "API": api is not None,
            "AccountsPool": pool is not None,
        }
        if api is not None:
            for method in ("tweet_details", "tweet_replies", "favoriters", "retweeters"):
                probed[method] = callable(getattr(api, method, None))
        return probed

    def __init__(
        self,
        settings: TwscrapeSettings,
        session_dir: Path,
        *,
        module: Any = None,
        api: Any = None,
        accounts: list[tuple[str, ...]] | None = None,
    ) -> None:
        self.settings = settings
        self.session_dir = session_dir
        self._module = module
        self._api = api
        self._accounts = accounts
        self._init_lock = asyncio.Lock()
        self._init_error = ""

    def _db_path(self) -> Path:
        raw = self.settings.accounts_db.replace("${SESSION_DIR}", str(self.session_dir))
        return Path(raw)

    def _parse_accounts(self) -> list[tuple[str, ...]]:
        if self._accounts is not None:
            return self._accounts
        raw = os.environ.get(self.settings.account_source_env, "").strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                values = json.loads(raw)
            except json.JSONDecodeError:
                return []
            result = []
            for item in values:
                if isinstance(item, (list, tuple)) and len(item) >= 4:
                    result.append(tuple(str(part) for part in item[:4]))
                elif isinstance(item, dict) and item.get("username"):
                    result.append(
                        (
                            str(item.get("username", "")),
                            str(item.get("password", "")),
                            str(item.get("email", "")),
                            str(item.get("email_password", "")),
                        )
                    )
            return result
        result = []
        for line in raw.splitlines():
            parts = line.strip().split(":")
            if len(parts) >= 4:
                result.append((parts[0], parts[1], parts[2], ":".join(parts[3:])))
        return result

    def available(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "disabled in acquisition configuration"
        if self._api is not None:
            return True, ""
        if importlib.util.find_spec("twscrape") is None:
            return False, "twscrape is not installed"
        has_accounts = bool(self._parse_accounts())
        db = self._db_path()
        has_db = db.exists() and db.stat().st_size > 0
        if not has_accounts and not has_db:
            return False, "no twscrape account pool configured"
        return True, ""

    async def _ensure_api(self) -> Any:
        if self._api is not None:
            return self._api
        async with self._init_lock:
            if self._api is not None:
                return self._api
            if self._init_error:
                raise CredentialsMissing(self._init_error, adapter=self.name)
            try:
                if self._module is None:
                    import twscrape  # type: ignore

                    self._module = twscrape
                self._db_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                pool = self._module.AccountsPool(str(self._db_path()))
                for account in self._parse_accounts():
                    try:  # noqa: S112 - invalid rows may contain credentials and are not logged
                        await pool.add_account(*account)
                    except Exception:  # noqa: S112 - invalid rows may contain credentials
                        # Duplicate/invalid accounts are not echoed (they may contain
                        # credentials). twscrape's rotation still uses valid entries.
                        continue
                self._api = self._module.API(pool)
                try:
                    os.chmod(self._db_path(), 0o600)
                except OSError:
                    pass
            except Exception as exc:
                self._init_error = f"twscrape could not start: {type(exc).__name__}"
                raise CredentialsMissing(self._init_error, adapter=self.name) from exc
        return self._api

    def capabilities(self) -> dict[str, bool]:
        return {
            "poll": True,
            "replies": True,
            "individual_likers": True,
            "individual_reposters": True,
            "auth_required": True,
        }

    async def _call(self, operation: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except TimeoutError as exc:
            raise SourceUnavailableError(f"{operation} timed out", adapter=self.name) from exc
        except Exception as exc:
            name = type(exc).__name__.lower()
            message = str(exc).lower()
            if "ratelimit" in name or "rate limit" in message or "too many requests" in message:
                raise RateLimitError(f"{operation} was rate limited", adapter=self.name) from exc
            if "unauthorized" in message or "login" in message or "auth" in name:
                raise CredentialsMissing(
                    f"{operation} was refused by the account pool", adapter=self.name
                ) from exc
            raise CollectionError(
                f"{operation} failed: {type(exc).__name__}", adapter=self.name
            ) from exc

    async def fetch_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        api = await self._ensure_api()
        tweet = await self._call("tweet_details", api.tweet_details, int(tweet_id))
        if tweet is None:
            return None
        author = _get(tweet, "user", {})
        metrics = {
            "likes": int(_get(tweet, "likeCount", 0) or 0),
            "reposts": int(_get(tweet, "retweetCount", 0) or 0),
            "replies": int(_get(tweet, "replyCount", 0) or 0),
            "quotes": int(_get(tweet, "quoteCount", 0) or 0),
        }
        for key, field in (("views", "viewCount"), ("bookmarks", "bookmarkCount")):
            if _get(tweet, field, None) is not None:
                metrics[key] = int(_get(tweet, field, 0) or 0)
        return {
            "post_id": str(_get(tweet, "id", tweet_id)),
            "url": str(_get(tweet, "url", f"https://x.com/i/web/status/{tweet_id}")),
            "text": str(_get(tweet, "rawContent", "") or _get(tweet, "content", "")),
            "created_at": _to_iso(_get(tweet, "date")),
            "author_id": str(_get(author, "id", "")),
            "author_username": str(_get(author, "username", "")),
            "metrics": metrics,
            "source_tool": self.name,
            "auth_tier": self.auth_tier,
            "raw_evidence_reference": "twscrape:tweet_details",
        }

    async def fetch_replies(self, tweet_id: str, limit: int = -1) -> list[NormalizedReply]:
        api = await self._ensure_api()
        replies: list[NormalizedReply] = []
        try:
            iterator = api.tweet_replies(int(tweet_id))
            async for tweet in iterator:
                author = _get(tweet, "user", {})
                replies.append(
                    NormalizedReply(
                        reply_id=str(_get(tweet, "id", "")),
                        author_id=str(_get(author, "id", "")),
                        author_username=str(_get(author, "username", "")),
                        text=str(_get(tweet, "rawContent", "") or _get(tweet, "content", "")),
                        created_at=_to_iso(_get(tweet, "date")),
                        url=str(
                            _get(tweet, "url", "")
                            or f"https://x.com/i/web/status/{_get(tweet, 'id', '')}"
                        ),
                        parent_post_id=tweet_id,
                        source_tool=self.name,
                        auth_tier=self.auth_tier,
                        raw_evidence_reference=f"twscrape:replies:{tweet_id}",
                    )
                )
                if limit >= 0 and len(replies) >= limit:
                    break
        except Exception as exc:
            name = type(exc).__name__.lower()
            if "ratelimit" in name or "rate limit" in str(exc).lower():
                raise RateLimitError(
                    "reply collection was rate limited", adapter=self.name
                ) from exc
            raise CollectionError(
                f"reply collection failed: {type(exc).__name__}", adapter=self.name
            ) from exc
        return replies

    async def fetch_poll(self, tweet_id: str) -> NormalizedPoll | None:
        api = await self._ensure_api()
        tweet = await self._call("tweet_details for poll", api.tweet_details, int(tweet_id))
        if tweet is None:
            return None
        card = _get(tweet, "card", None)
        if card is None:
            return None
        options_raw = _get(card, "options", []) or []
        options: list[dict[str, Any]] = []
        for option in options_raw:
            label = str(_get(option, "label", "") or _get(option, "choice", ""))
            votes = _get(option, "votesCount", _get(option, "votes", 0))
            try:
                votes_int = max(0, int(votes or 0))
            except (TypeError, ValueError):
                votes_int = 0
            if label:
                options.append({"label": label, "votes": votes_int})
        if not options:
            return None
        total = sum(option["votes"] for option in options)
        finished = bool(_get(card, "finished", False))
        limitations = []
        if not finished:
            limitations.append("Poll was still open when observed; counts may change.")
        limitations.append(
            "Native poll votes are anonymous aggregates; individual voters are not observable."
        )
        return NormalizedPoll(
            post_id=str(_get(tweet, "id", tweet_id)),
            options=options,
            total_votes=total,
            finished=finished,
            collected_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source_tool=self.name,
            auth_tier=self.auth_tier,
            raw_evidence_reference=f"twscrape:card:{tweet_id}",
            url=f"https://x.com/i/web/status/{tweet_id}",
            exact_counts=True,
            limitations=limitations,
        )

    async def _fetch_accounts(self, tweet_id: str, method: str, limit: int) -> list[dict[str, Any]]:
        api = await self._ensure_api()
        method_func = getattr(api, method)
        accounts: list[dict[str, Any]] = []
        try:
            async for user in method_func(int(tweet_id)):
                accounts.append(
                    {
                        "account_id": str(_get(user, "id", "")),
                        "username": str(_get(user, "username", "")),
                        "source_tool": self.name,
                        "auth_tier": self.auth_tier,
                        "raw_evidence_reference": f"twscrape:{method}:{tweet_id}",
                    }
                )
                if limit >= 0 and len(accounts) >= limit:
                    break
        except Exception as exc:
            name = type(exc).__name__.lower()
            if "ratelimit" in name or "rate limit" in str(exc).lower():
                raise RateLimitError(f"{method} was rate limited", adapter=self.name) from exc
            raise CollectionError(
                f"{method} failed: {type(exc).__name__}", adapter=self.name
            ) from exc
        return accounts

    async def fetch_likers(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        return await self._fetch_accounts(tweet_id, "favoriters", limit)

    async def fetch_reposters(self, tweet_id: str, limit: int = -1) -> list[dict[str, Any]]:
        return await self._fetch_accounts(tweet_id, "retweeters", limit)
