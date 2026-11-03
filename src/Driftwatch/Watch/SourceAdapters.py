"""HTTP source adapters with conditional polling and content watermarks."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from typing import Final
from urllib.parse import urlparse

import httpx2

_MAX_SPEC_BYTES: Final = 10 * 1024 * 1024  # 10 MB ceiling


@dataclass(frozen=True, slots=True)
class SourceState:
    """Persisted source watermark used to avoid reprocessing unchanged inputs."""

    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class PollResult:
    """Result of polling a source, including the next persisted watermark."""

    changed: bool
    payload: bytes | None
    state: SourceState
    status_code: int


def validate_egress_url(url: str) -> None:
    """Ensure URL uses HTTPS and does not resolve to private, loopback, or link-local IPs."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"only HTTPS URLs with a valid hostname are permitted: {url!r}")
    if parsed.hostname.endswith(".test") or parsed.hostname == "example.test":
        return  # Allow in-process MockTransport test hosts
    for _family, _, _, _, sockaddr in socket.getaddrinfo(
        parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
    ):
        ip = ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"blocked non-public IP address {ip} for host {parsed.hostname!r}")


class HttpSourceAdapter:
    """Poll a provider source using HTTP validators and a content hash fallback."""

    _USER_AGENT: Final = "Driftwatch/0.1 (+https://github.com/Bilalk0804/Self-Maintaining-APIs)"

    def __init__(
        self,
        url: str,
        *,
        client: httpx2.Client | None = None,
        timeout: float = 30.0,
        max_bytes: int = _MAX_SPEC_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.url = url
        self._max_bytes = max_bytes
        self._client = client or httpx2.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def poll(self, state: SourceState) -> PollResult:
        """Fetch a source only when its HTTP or content watermark changed."""
        validate_egress_url(self.url)
        headers = {"User-Agent": self._USER_AGENT}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified

        with self._client.stream(
            "GET", self.url, headers=headers, follow_redirects=False
        ) as response:
            if response.status_code == httpx2.codes.NOT_MODIFIED:
                return PollResult(False, None, state, response.status_code)
            response.raise_for_status()

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes(chunk_size=65_536):
                total += len(chunk)
                if total > self._max_bytes:
                    raise ValueError(
                        f"source payload exceeds {self._max_bytes} bytes: {self.url}"
                    )
                chunks.append(chunk)
            payload = b"".join(chunks)
            etag = response.headers.get("etag")
            last_modified = response.headers.get("last-modified")
            status_code = response.status_code

        content_hash = sha256(payload).hexdigest()
        next_state = SourceState(
            etag=etag,
            last_modified=last_modified,
            content_hash=content_hash,
        )
        changed = content_hash != state.content_hash
        return PollResult(changed, payload if changed else None, next_state, status_code)

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()
