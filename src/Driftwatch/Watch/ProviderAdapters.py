"""Provider-specific source adapters built on the shared HTTP polling contract."""

from __future__ import annotations

from typing import Protocol

import httpx2

from Driftwatch.Providers import Provider, ProviderSource, get_provider
from Driftwatch.Watch.SourceAdapters import HttpSourceAdapter, PollResult, SourceState


class ProviderAdapter(Protocol):
    """Poll one configured source for a provider."""

    provider: str
    source: ProviderSource

    def poll(self, state: SourceState) -> PollResult:
        """Return a bounded, watermark-aware source result."""


class ConfiguredProviderAdapter:
    """Bind provider metadata to the shared HTTP source implementation."""

    def __init__(
        self,
        provider: Provider,
        source: ProviderSource,
        *,
        client: httpx2.Client | None = None,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if source not in provider.sources:
            raise ValueError(f"source is not configured for provider {provider.slug!r}")
        self.provider = provider.slug
        self.source = source
        self._source_adapter = HttpSourceAdapter(
            source.url,
            client=client,
            max_bytes=max_bytes,
        )

    def poll(self, state: SourceState) -> PollResult:
        """Poll only the configured provider source."""
        return self._source_adapter.poll(state)

    def close(self) -> None:
        """Release the underlying HTTP client when it is owned."""
        self._source_adapter.close()


class OpenAIProviderAdapter(ConfiguredProviderAdapter):
    """Poll OpenAI's configured ground-truth or narrative source."""

    def __init__(
        self,
        source: ProviderSource,
        *,
        client: httpx2.Client | None = None,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        super().__init__(
            get_provider("openai"),
            source,
            client=client,
            max_bytes=max_bytes,
        )


class RazorpayProviderAdapter(ConfiguredProviderAdapter):
    """Poll Razorpay's SDK-source or changelog source."""

    def __init__(
        self,
        source: ProviderSource,
        *,
        client: httpx2.Client | None = None,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        super().__init__(
            get_provider("razorpay"),
            source,
            client=client,
            max_bytes=max_bytes,
        )


def provider_adapter(
    provider: str,
    source_kind: str,
    *,
    client: httpx2.Client | None = None,
    max_bytes: int = 10 * 1024 * 1024,
) -> ConfiguredProviderAdapter:
    """Create an adapter from stable provider and source-kind configuration."""
    metadata = get_provider(provider)
    try:
        source = next(source for source in metadata.sources if source.kind == source_kind)
    except StopIteration as error:
        raise ValueError(
            f"provider {provider!r} has no configured {source_kind!r} source"
        ) from error
    return ConfiguredProviderAdapter(metadata, source, client=client, max_bytes=max_bytes)
