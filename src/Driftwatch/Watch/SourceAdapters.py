"""HTTP source adapters with conditional polling and content watermarks."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final

import httpx


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


class HttpSourceAdapter:
    """Poll a provider source using HTTP validators and a content hash fallback."""

    _USER_AGENT: Final = "Driftwatch/0.1 (+https://github.com/Bilalk0804/Self-Maintaining-APIs)"

    def __init__(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def poll(self, state: SourceState) -> PollResult:
        """Fetch a source only when its HTTP or content watermark changed."""
        headers = {"User-Agent": self._USER_AGENT}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified

        response = self._client.get(self.url, headers=headers)
        if response.status_code == httpx.codes.NOT_MODIFIED:
            return PollResult(False, None, state, response.status_code)
        response.raise_for_status()

        payload = response.content
        content_hash = sha256(payload).hexdigest()
        next_state = SourceState(
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            content_hash=content_hash,
        )
        changed = content_hash != state.content_hash
        return PollResult(changed, payload if changed else None, next_state, response.status_code)

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()
