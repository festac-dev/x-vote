from __future__ import annotations

import json
import os

import pytest

from xvote.config import load_acquisition_config, load_env_file, redact


def test_loader_clamps_unsafe_values_and_resolves_sections(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
acquisition:
  preference_order: [x-cli]
  x_tweet_fetcher:
    enabled: false
    nitter_base: "http://127.0.0.1:9999/"
collection:
  max_concurrency: 0
  retry_limit: 99
  max_events_in_memory: 1
agents:
  ai_mode: unexpected
  max_concurrent_tasks: 99
  max_depth: 0
compliance:
  require_authorized_use_ack: false
""",
        encoding="utf-8",
    )
    config = load_acquisition_config(path)
    assert config.preference_order == ["x-cli"]
    assert config.x_tweet_fetcher.enabled is False
    assert config.x_tweet_fetcher.nitter_base == "http://127.0.0.1:9999"
    assert config.max_concurrency == 1
    assert config.retry_limit == 5
    assert config.max_events_in_memory == 100
    assert config.ai_mode == "none"
    assert config.agents_max_concurrent == 8
    assert config.agents_max_depth == 1
    assert config.authorized_use_acknowledged is True


def test_error_loading_config_is_wrapped(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("acquisition: [unclosed", encoding="utf-8")
    try:
        load_acquisition_config(path)
    except ValueError as exc:
        assert "cannot read acquisition configuration" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_compliance_acknowledgement_is_environment_driven(monkeypatch) -> None:
    monkeypatch.delenv("XVOTE_AUTHORIZED_USE_ACK", raising=False)
    config = load_acquisition_config()
    assert config.authorized_use_acknowledged is False
    monkeypatch.setenv("XVOTE_AUTHORIZED_USE_ACK", "YES")
    assert config.authorized_use_acknowledged is True


def test_ai_is_only_configured_with_consent_endpoint_and_remote_key(monkeypatch) -> None:
    monkeypatch.setenv("XVOTE_AI_ENDPOINT", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("XVOTE_AI_CONSENT", "yes")
    monkeypatch.setenv("XVOTE_AI_KEY", "unit-test-secret-key")
    config = load_acquisition_config()
    config.ai_mode = "local"
    assert config.ai_configured is True
    config.ai_mode = "remote"
    assert config.ai_configured is True
    monkeypatch.delenv("XVOTE_AI_KEY")
    assert config.ai_configured is False
    monkeypatch.setenv("XVOTE_AI_KEY", "unit-test-secret-key")
    monkeypatch.setenv("XVOTE_AI_CONSENT", "no")
    assert config.ai_configured is False


def test_public_dict_omits_credentials_and_endpoints(monkeypatch) -> None:
    monkeypatch.setenv("XVOTE_AI_ENDPOINT", "http://private.example.invalid/v1")
    monkeypatch.setenv("XVOTE_AI_KEY", "super-secret-ai-key")
    config = load_acquisition_config()
    payload = json.dumps(config.public_dict())
    assert "private.example.invalid" not in payload
    assert "super-secret-ai-key" not in payload
    assert config.public_dict()["compliance"]["no_captcha_or_auth_bypass"] is True


def test_redact_short_and_long_values() -> None:
    assert redact("") == ""
    assert redact("short") == "***"
    assert redact("abcdefghij") == "ab***ij"


def test_source_project_name_resolves_to_optional_project_id() -> None:
    from xvote.models import Competition

    competition = Competition.from_dict(
        {
            "name": "Project source",
            "projects": [{"name": "Alpha"}],
            "sources": [
                {
                    "source_type": "likes",
                    "project_name": "Alpha",
                    "methods": ["like"],
                }
            ],
            "methods": ["like"],
        }
    )
    assert competition.sources[0].project_id == competition.projects[0].project_id
    with pytest.raises(ValueError, match="unknown project"):
        Competition.from_dict(
            {
                "name": "Bad source",
                "projects": [{"name": "Alpha"}],
                "sources": [{"source_type": "likes", "project_name": "Beta"}],
                "methods": ["like"],
            }
        )


def test_load_env_file_preserves_password_hashes_and_existing_values(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "export XVOTE_AUTHORIZED_USE_ACK=yes\n"
        "TWSCRAPE_ACCOUNTS=user:pa#ss:mail@example.com:pa#ss\n"
        "QUOTED='two words'\n"
        "KEEP=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("XVOTE_AUTHORIZED_USE_ACK", raising=False)
    monkeypatch.delenv("TWSCRAPE_ACCOUNTS", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.setenv("KEEP", "from-process")
    assert load_env_file(env_path) == env_path
    assert os.environ["XVOTE_AUTHORIZED_USE_ACK"] == "yes"
    assert os.environ["TWSCRAPE_ACCOUNTS"] == "user:pa#ss:mail@example.com:pa#ss"
    assert os.environ["QUOTED"] == "two words"
    assert os.environ["KEEP"] == "from-process"
