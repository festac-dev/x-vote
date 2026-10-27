"""Configuration loading with environment-only credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "acquisition.yaml"

SECRET_ENV_NAMES = (
    "TWSCRAPE_ACCOUNTS",
    "SCWEET_AUTH_TOKEN",
    "XCLI_SESSION_COOKIE",
    "XVOTE_AI_KEY",
)
# Loaded by the CLI before dispatch; existing process environment wins.


def redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:2] + "***" + value[-2:]


def env_present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def load_env_file(path: str | Path | None = None) -> Path | None:
    """Load KEY=VALUE pairs from a local .env without overriding exported values.

    Values are never printed. Inline ``#`` characters are preserved because
    passwords commonly contain them; quote a whole value if it needs spaces.
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        configured = os.environ.get("XVOTE_ENV_FILE", "").strip()
        if configured:
            candidates.append(Path(configured))
        else:
            candidates.extend([Path.cwd() / ".env", ROOT / ".env"])
    selected = next((item for item in candidates if item.exists()), None)
    if selected is None:
        return None
    try:
        lines = selected.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read environment file {selected}: {exc}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return selected


@dataclass
class XTweetFetcherSettings:
    enabled: bool = True
    nitter_base: str = "http://127.0.0.1:8788"
    request_timeout_seconds: float = 15.0
    max_response_bytes: int = 2_097_152


@dataclass
class XCliSettings:
    enabled: bool = True
    binary: str = "x"
    session_cookie_env: str = "XCLI_SESSION_COOKIE"
    timeout_seconds: float = 30.0


@dataclass
class TwscrapeSettings:
    enabled: bool = True
    accounts_db: str = "${SESSION_DIR}/twscrape_accounts.db"
    account_source_env: str = "TWSCRAPE_ACCOUNTS"


@dataclass
class ScweetSettings:
    enabled: bool = False
    auth_token_env: str = "SCWEET_AUTH_TOKEN"  # noqa: S105 - env var name, not a secret


@dataclass
class AcquisitionConfig:
    preference_order: list[str] = field(
        default_factory=lambda: ["x-tweet-fetcher", "x-cli", "twscrape", "Scweet"]
    )
    x_tweet_fetcher: XTweetFetcherSettings = field(default_factory=XTweetFetcherSettings)
    x_cli: XCliSettings = field(default_factory=XCliSettings)
    twscrape: TwscrapeSettings = field(default_factory=TwscrapeSettings)
    scweet: ScweetSettings = field(default_factory=ScweetSettings)
    max_concurrency: int = 3
    per_task_timeout_seconds: float = 45.0
    retry_limit: int = 2
    max_events_in_memory: int = 2000
    agents_max_concurrent: int = 3
    agents_max_depth: int = 3
    ai_mode: str = "none"
    ai_endpoint_env: str = "XVOTE_AI_ENDPOINT"
    ai_key_env: str = "XVOTE_AI_KEY"
    ai_consent_env: str = "XVOTE_AI_CONSENT"
    ai_model: str = "local-model"
    ai_auth_header: str = "Authorization"
    ai_auth_prefix: str = ""
    authorized_use_env: str = "XVOTE_AUTHORIZED_USE_ACK"
    require_authorized_use_ack: bool = True

    @property
    def ai_endpoint(self) -> str:
        return os.environ.get(self.ai_endpoint_env, "").strip()

    @property
    def ai_configured(self) -> bool:
        if self.ai_mode == "none":
            return False
        if os.environ.get(self.ai_consent_env, "").strip().lower() not in {"yes", "true", "1"}:
            return False
        if not self.ai_endpoint:
            return False
        if self.ai_mode == "remote" and not env_present(self.ai_key_env):
            return False
        return True

    @property
    def authorized_use_acknowledged(self) -> bool:
        if not self.require_authorized_use_ack:
            return True
        return os.environ.get(self.authorized_use_env, "").strip().lower() in {"yes", "true", "1"}

    def public_dict(self) -> dict[str, Any]:
        """Serializable summary that omits credentials and endpoint values."""
        return {
            "preference_order": list(self.preference_order),
            "max_concurrency": self.max_concurrency,
            "per_task_timeout_seconds": self.per_task_timeout_seconds,
            "retry_limit": self.retry_limit,
            "agents_max_concurrent": self.agents_max_concurrent,
            "agents_max_depth": self.agents_max_depth,
            "ai_mode": self.ai_mode,
            "ai_configured": self.ai_configured,
            "authorized_use_acknowledged": self.authorized_use_acknowledged,
            "compliance": {
                "authorized_sessions_only": True,
                "no_captcha_or_auth_bypass": True,
                "respect_rate_limits": True,
                "note": (
                    "Authorized sessions only. Respect tool licenses, X's applicable terms and "
                    "access controls, and self-hosted instance policies. Open-source tooling does "
                    "not make every acquisition method permitted."
                ),
            },
        }


def load_acquisition_config(path: str | Path | None = None) -> AcquisitionConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot read acquisition configuration: {exc}") from exc

    acquisition = data.get("acquisition") or {}
    collection = data.get("collection") or {}
    agents = data.get("agents") or {}
    compliance = data.get("compliance") or {}
    if not isinstance(compliance, dict):
        compliance = {}

    def section(name: str, fallback: dict[str, Any]) -> dict[str, Any]:
        value = acquisition.get(name) or {}
        return value if isinstance(value, dict) else fallback

    xtf = section("x_tweet_fetcher", {})
    xcli = section("x_cli", {})
    tw = section("twscrape", {})
    sc = section("scweet", {})

    preference = acquisition.get("preference_order") or [
        "x-tweet-fetcher",
        "x-cli",
        "twscrape",
        "Scweet",
    ]
    if not isinstance(preference, list):
        preference = ["x-tweet-fetcher", "x-cli", "twscrape", "Scweet"]

    ai_mode = str(agents.get("ai_mode", "none")).lower()
    if ai_mode not in {"none", "local", "remote"}:
        ai_mode = "none"

    return AcquisitionConfig(
        preference_order=[str(item) for item in preference],
        x_tweet_fetcher=XTweetFetcherSettings(
            enabled=bool(xtf.get("enabled", True)),
            nitter_base=str(xtf.get("nitter_base") or "http://127.0.0.1:8788").rstrip("/"),
            request_timeout_seconds=float(xtf.get("request_timeout_seconds", 15)),
            max_response_bytes=int(xtf.get("max_response_bytes", 2_097_152)),
        ),
        x_cli=XCliSettings(
            enabled=bool(xcli.get("enabled", True)),
            binary=str(xcli.get("binary") or "x"),
            session_cookie_env=str(xcli.get("session_cookie_env") or "XCLI_SESSION_COOKIE"),
            timeout_seconds=float(xcli.get("timeout_seconds", 30)),
        ),
        twscrape=TwscrapeSettings(
            enabled=bool(tw.get("enabled", True)),
            accounts_db=str(tw.get("accounts_db") or "${SESSION_DIR}/twscrape_accounts.db"),
            account_source_env=str(tw.get("account_source_env") or "TWSCRAPE_ACCOUNTS"),
        ),
        scweet=ScweetSettings(
            enabled=bool(sc.get("enabled", False)),
            auth_token_env=str(sc.get("auth_token_env") or "SCWEET_AUTH_TOKEN"),
        ),
        max_concurrency=max(1, int(collection.get("max_concurrency", 3))),
        per_task_timeout_seconds=max(1.0, float(collection.get("per_task_timeout_seconds", 45))),
        retry_limit=max(0, min(5, int(collection.get("retry_limit", 2)))),
        max_events_in_memory=max(100, int(collection.get("max_events_in_memory", 2000))),
        agents_max_concurrent=max(1, min(8, int(agents.get("max_concurrent_tasks", 3)))),
        agents_max_depth=max(1, min(4, int(agents.get("max_depth", 3)))),
        ai_mode=ai_mode,
        ai_endpoint_env=str(agents.get("ai_endpoint_env") or "XVOTE_AI_ENDPOINT"),
        ai_key_env=str(agents.get("ai_key_env") or "XVOTE_AI_KEY"),
        ai_consent_env=str(agents.get("ai_consent_env") or "XVOTE_AI_CONSENT"),
        ai_model=str(agents.get("ai_model") or "local-model"),
        ai_auth_header=str(agents.get("ai_auth_header") or "Authorization"),
        ai_auth_prefix=str(agents.get("ai_auth_prefix") or ""),
        authorized_use_env=str(compliance.get("authorized_use_env") or "XVOTE_AUTHORIZED_USE_ACK"),
        require_authorized_use_ack=bool(compliance.get("require_authorized_use_ack", True)),
    )
