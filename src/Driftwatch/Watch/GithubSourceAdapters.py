"""Ground-truth payload extraction from GitHub repository archives."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterable
from hashlib import sha256
from typing import Final
from urllib.parse import urlparse

import httpx2
import orjson

from Driftwatch.Watch.SourceAdapters import PollResult, SourceState

_MAX_ARCHIVE_BYTES: Final = 100 * 1024 * 1024


class GithubRepositorySourceAdapter:
    """Extract indexable provider payloads from a GitHub default-branch archive."""

    def __init__(
        self,
        repository_url: str,
        source_kind: str,
        *,
        client: httpx2.Client | None = None,
        max_bytes: int = _MAX_ARCHIVE_BYTES,
    ) -> None:
        owner, repository = _repository_parts(repository_url)
        if source_kind not in {"openapi", "sdk_source"}:
            raise ValueError(f"GitHub repository source kind is not indexable: {source_kind}")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.url = repository_url
        self.source_kind = source_kind
        self._archive_url = f"https://codeload.github.com/{owner}/{repository}/tar.gz/HEAD"
        self._max_bytes = max_bytes
        self._client = client or httpx2.Client(timeout=30, trust_env=False)
        self._owns_client = client is None

    def poll(self, state: SourceState) -> PollResult:
        """Download an archive only when its HTTP validator or extracted content changes."""
        headers = {"User-Agent": "Driftwatch/0.1"}
        if state.etag:
            headers["If-None-Match"] = state.etag
        with self._client.stream("GET", self._archive_url, headers=headers) as response:
            if response.status_code == httpx2.codes.NOT_MODIFIED:
                return PollResult(False, None, state, response.status_code)
            response.raise_for_status()
            archive = _read_bounded(response.iter_bytes(65_536), self._max_bytes)
            etag = response.headers.get("etag")
            status_code = response.status_code
        payload = _extract_payload(archive, self.source_kind)
        content_hash = sha256(payload).hexdigest()
        next_state = SourceState(etag=etag, content_hash=content_hash)
        changed = content_hash != state.content_hash
        return PollResult(changed, payload if changed else None, next_state, status_code)

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()


def _repository_parts(repository_url: str) -> tuple[str, str]:
    parsed = urlparse(repository_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError(f"expected an HTTPS GitHub repository URL: {repository_url!r}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or any(part.endswith(".git") for part in parts):
        raise ValueError(f"expected a GitHub owner/repository URL: {repository_url!r}")
    return parts[0], parts[1]


def _read_bounded(chunks: Iterable[bytes], max_bytes: int) -> bytes:
    payload: list[bytes] = []
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"GitHub archive exceeds {max_bytes} bytes")
        payload.append(chunk)
    return b"".join(payload)


def _extract_payload(archive: bytes, source_kind: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = [member for member in bundle.getmembers() if member.isfile()]
        if source_kind == "sdk_source":
            files = sorted(
                member
                for member in members
                if member.name.endswith(".py")
                and "/test" not in member.name.lower()
                and "/tests/" not in member.name.lower()
            )
            if not files:
                raise ValueError("GitHub SDK archive contained no Python source files")
            return b"\n\n".join(bundle.extractfile(member).read() for member in files)  # type: ignore[union-attr]

        documents: list[dict[str, object]] = []
        for member in members:
            if not member.name.endswith(".json"):
                continue
            raw = bundle.extractfile(member)
            if raw is None:
                continue
            document = orjson.loads(raw.read())
            if isinstance(document, dict) and isinstance(document.get("paths"), dict):
                documents.append(document)
        if not documents:
            raise ValueError("GitHub archive contained no OpenAPI JSON files")
        merged: dict[str, object] = {
            "openapi": documents[0].get("openapi", "3.0.0"),
            "info": documents[0].get(
                "info", {"title": "Merged provider API", "version": "archive"}
            ),
            "paths": {},
        }
        paths = merged["paths"]
        if not isinstance(paths, dict):
            raise ValueError("OpenAPI archive paths must be an object")
        for document in documents:
            paths.update(document["paths"])  # type: ignore[union-attr]
        return orjson.dumps(merged)
