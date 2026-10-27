from __future__ import annotations

import pytest

from xvote.urls import OutboundFetchError, parse_x_post_reference, validate_outbound_url


@pytest.mark.parametrize(
    ("value", "post_id", "username"),
    [
        ("https://x.com/alpha/status/1234567890123456789", "1234567890123456789", "alpha"),
        ("https://twitter.com/alpha/statuses/42?s=20", "42", "alpha"),
        ("http://mobile.x.com/i/web/status/99", "99", ""),
        ("123456", "123456", ""),
    ],
)
def test_parse_x_post_reference_accepts_canonical_forms(
    value: str, post_id: str, username: str
) -> None:
    reference = parse_x_post_reference(value)
    assert reference.post_id == post_id
    assert reference.username == username
    assert reference.canonical_url.startswith("https://x.com/")


@pytest.mark.parametrize(
    "value",
    [
        "https://x.com.evil.test/alpha/status/123",
        "https://evil.test/alpha/status/123",
        "https://x.com@evil.test/alpha/status/123",
        "https://user:pass@x.com/alpha/status/123",
        "https://x.com:8443/alpha/status/123",
        "https://x.com/alpha/status/not-a-number",
        "ftp://x.com/alpha/status/123",
        "https://x.com/alpha",
    ],
)
def test_parse_x_post_reference_rejects_lookalikes_and_non_status_urls(value: str) -> None:
    with pytest.raises(ValueError):
        parse_x_post_reference(value)


def test_outbound_validation_requires_allowlisted_host_and_safe_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    assert (
        validate_outbound_url("https://api.fxtwitter.com/status/1", {"api.fxtwitter.com"})
        == "https://api.fxtwitter.com/status/1"
    )
    with pytest.raises(OutboundFetchError):
        validate_outbound_url("file:///etc/passwd", {"api.fxtwitter.com"})
    with pytest.raises(OutboundFetchError):
        validate_outbound_url("https://user:pass@api.fxtwitter.com/status/1", {"api.fxtwitter.com"})
    with pytest.raises(OutboundFetchError):
        validate_outbound_url("https://evil.test/status/1", {"api.fxtwitter.com"})
