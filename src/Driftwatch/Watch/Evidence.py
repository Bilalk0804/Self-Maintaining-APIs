"""Retrieve quote-grounded evidence for deterministic API deltas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

import httpx

from Driftwatch.Providers import ProviderSource
from Driftwatch.Surfaces import SurfaceRef
from Driftwatch.Watch.Differ import Delta
from Driftwatch.Watch.SourceAdapters import validate_egress_url

EvidenceKind = Literal["raw_delta", "provider_source"]


@dataclass(frozen=True, slots=True)
class Evidence:
    """A verbatim quote that can support a change classification."""

    kind: EvidenceKind
    ref: str
    quote: str
    source_url: str | None = None
    locator: str | None = None
    retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        """Reject evidence that cannot be independently checked."""
        if not self.quote.strip():
            raise ValueError("evidence quote must not be empty")
        if self.kind == "provider_source" and not self.source_url:
            raise ValueError("provider source evidence requires source_url")


class EvidenceRetriever(Protocol):
    """Retrieve evidence records for a deterministic delta."""

    def retrieve(self, delta: Delta) -> list[Evidence]:
        """Return verbatim evidence supporting the delta."""


class HttpEvidenceRetriever:
    """Retrieve bounded, line-based quotes from configured provider sources."""

    def __init__(
        self,
        sources: tuple[ProviderSource, ...],
        *,
        client: httpx.Client | None = None,
        max_bytes: int = 2_000_000,
        max_quotes_per_source: int = 2,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_quotes_per_source <= 0:
            raise ValueError("max_quotes_per_source must be positive")
        self._sources = sources
        self._client = client or httpx.Client(
            timeout=30.0,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._max_bytes = max_bytes
        self._max_quotes_per_source = max_quotes_per_source

    def retrieve(self, delta: Delta) -> list[Evidence]:
        """Return the raw delta and matching provider-source quotes."""
        evidence = [
            Evidence(
                kind="raw_delta",
                ref=delta.fingerprint,
                quote=delta.detail or f"{delta.change_type} on {delta.surface_ref}",
            )
        ]
        surface = SurfaceRef.parse(delta.surface_ref)
        probes = [
            probe.casefold()
            for probe in [*delta.code_probes(), surface.ident, surface.subident or ""]
            if probe.strip()
        ]
        for source in self._sources:
            validate_egress_url(source.url)
            with self._client.stream("GET", source.url, follow_redirects=False) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(chunk_size=65_536):
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise ValueError(
                            f"evidence source exceeds {self._max_bytes} bytes: {source.url}"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                encoding = response.encoding or "utf-8"
            text = body.decode(encoding, errors="replace")
            evidence.extend(self._source_matches(source, text, probes, delta))
        return evidence

    def close(self) -> None:
        """Close the HTTP client when this retriever owns it."""
        if self._owns_client:
            self._client.close()

    def _source_matches(
        self,
        source: ProviderSource,
        text: str,
        probes: list[str],
        delta: Delta,
    ) -> list[Evidence]:
        """Extract bounded verbatim line windows containing delta probes."""
        lines = text.splitlines()
        matches: list[Evidence] = []
        seen_quotes: set[str] = set()
        for line_number, line in enumerate(lines):
            if not any(probe in line.casefold() for probe in probes):
                continue
            start = max(0, line_number - 1)
            end = min(len(lines), line_number + 2)
            quote = "\n".join(lines[start:end]).strip()
            if not quote or quote in seen_quotes:
                continue
            seen_quotes.add(quote)
            matches.append(
                Evidence(
                    kind="provider_source",
                    ref=f"{delta.fingerprint}:{source.kind}:{line_number + 1}",
                    quote=quote,
                    source_url=source.url,
                    locator=f"line {line_number + 1}",
                    retrieved_at=datetime.now(UTC),
                )
            )
            if len(matches) >= self._max_quotes_per_source:
                break
        return matches
