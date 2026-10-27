"""Safe parsing of X references and guarded outbound HTTP."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "m.twitter.com",
}

_STATUS_PATH = __import__("re").compile(
    r"^/(?:(?:i/web|[A-Za-z0-9_]{1,15})/)?status(?:es)?/(\d{1,25})(?:/.*)?$"
)
_POST_ID = __import__("re").compile(r"^\d{1,25}$")


@dataclass(frozen=True)
class XPostReference:
    post_id: str
    username: str
    canonical_url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "post_id": self.post_id,
            "username": self.username,
            "canonical_url": self.canonical_url,
        }


def parse_x_post_reference(value: str) -> XPostReference:
    """Extract a post ID and canonical URL from an X URL or bare numeric ID.

    Only x.com/twitter.com status URLs and bare IDs are accepted. Lookalike
    domains (e.g. x.com.evil.test), userinfo tricks and non-status paths are
    rejected rather than repaired.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("an X post URL or numeric post ID is required")
    if _POST_ID.fullmatch(text):
        return XPostReference(
            post_id=text,
            username="",
            canonical_url=f"https://x.com/i/web/status/{text}",
        )
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise ValueError("malformed URL") from exc
    if parts.scheme not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not accepted")
    if parts.port not in {None, 80, 443}:
        raise ValueError("non-standard ports are not accepted for X references")
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in X_HOSTS:
        raise ValueError("only x.com or twitter.com status URLs are accepted")
    match = _STATUS_PATH.match(parts.path or "")
    if not match:
        raise ValueError("URL does not point to an X status")
    post_id = match.group(1)
    segments = [segment for segment in parts.path.split("/") if segment]
    username = ""
    if len(segments) >= 3 and segments[0] not in {"i", "web"}:
        username = segments[0]
    canonical = (
        f"https://x.com/{username}/status/{post_id}"
        if username
        else f"https://x.com/i/web/status/{post_id}"
    )
    return XPostReference(post_id=post_id, username=username, canonical_url=canonical)


class OutboundFetchError(RuntimeError):
    pass


class ResponseTooLarge(OutboundFetchError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        raise OutboundFetchError(f"redirects are disabled (server returned {code})")


def validate_outbound_url(url: str, allowed_hosts: set[str]) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise OutboundFetchError("malformed outbound URL") from exc
    if parts.scheme not in {"http", "https"}:
        raise OutboundFetchError("outbound URL scheme must be http or https")
    if parts.username or parts.password:
        raise OutboundFetchError("outbound URLs must not embed credentials")
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in {item.lower() for item in allowed_hosts}:
        raise OutboundFetchError(f"host is not allowed for outbound collection: {host or 'empty'}")
    if not host:
        raise OutboundFetchError("outbound URL has no host")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise OutboundFetchError(f"cannot resolve outbound host: {host}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise OutboundFetchError(f"outbound host resolves to a blocked address: {address}")
    return url


def fetch_guarded(
    url: str,
    allowed_hosts: set[str],
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_097_152,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Fetch a URL with host allow-listing, no redirects, timeout and size cap."""
    validate_outbound_url(url, allowed_hosts)
    request = urllib.request.Request(  # noqa: S310 - scheme and host are validated first
        url,
        headers={
            "User-Agent": "x-vote/1.0 (read-only collection)",
            "Accept": "application/json,text/html;q=0.8",
            **(headers or {}),
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise OutboundFetchError(f"source returned HTTP {status}")
            length_header = response.headers.get("Content-Length")
            if length_header and int(length_header) > max_bytes:
                raise ResponseTooLarge("source response exceeds the configured size limit")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ResponseTooLarge("source response exceeds the configured size limit")
            charset = response.headers.get_content_charset() or "utf-8"
            return payload, charset
    except urllib.error.HTTPError as exc:
        raise OutboundFetchError(f"source returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as exc:
        raise OutboundFetchError(f"source request failed: {type(exc).__name__}") from exc


def fetch_json(
    url: str,
    allowed_hosts: set[str],
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_097_152,
) -> Any:
    payload, charset = fetch_guarded(url, allowed_hosts, timeout=timeout, max_bytes=max_bytes)
    try:
        return json.loads(payload.decode(charset or "utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutboundFetchError("source returned invalid JSON") from exc


def fetch_text(
    url: str,
    allowed_hosts: set[str],
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_097_152,
) -> str:
    payload, charset = fetch_guarded(url, allowed_hosts, timeout=timeout, max_bytes=max_bytes)
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def nitter_host(nitter_base: str) -> str:
    try:
        parts = urllib.parse.urlsplit(nitter_base)
    except ValueError as exc:
        raise ValueError("invalid Nitter base URL") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Nitter base must be an http(s) URL with a host")
    if parts.username or parts.password:
        raise ValueError("Nitter base must not embed credentials")
    return parts.hostname.lower()
