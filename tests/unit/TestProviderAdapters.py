from __future__ import annotations

import httpx2
import pytest

from Driftwatch.Providers import OPENAI, RAZORPAY, Provider, ProviderSource
from Driftwatch.Watch.ProviderAdapters import (
    ConfiguredProviderAdapter,
    OpenAIProviderAdapter,
    RazorpayProviderAdapter,
    provider_adapter,
)


def mock_client(payload: bytes = b"source") -> httpx2.Client:
    return httpx2.Client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=payload))
    )


def test_openai_repository_source_is_rejected_until_repository_adapter_exists() -> None:
    source = OPENAI.sources[0]

    with pytest.raises(ValueError, match="direct payload URL"):
        OpenAIProviderAdapter(source, client=mock_client(b"openapi"))


def test_razorpay_repository_source_is_rejected_until_repository_adapter_exists() -> None:
    source = RAZORPAY.sources[0]

    with pytest.raises(ValueError, match="direct payload URL"):
        RazorpayProviderAdapter(source, client=mock_client(b"sdk"))


def test_factory_selects_source_kind() -> None:
    adapter = provider_adapter("razorpay", "changelog_html", client=mock_client())

    assert adapter.provider == "razorpay"
    assert adapter.source.ground_truth is False


def test_factory_rejects_unconfigured_source_kind() -> None:
    with pytest.raises(ValueError, match="no configured"):
        provider_adapter("razorpay", "openapi", client=mock_client())


def test_adapter_rejects_source_from_another_provider() -> None:
    source = ProviderSource("openapi", "https://example.test/other", True)

    with pytest.raises(ValueError, match="not configured"):
        OpenAIProviderAdapter(source, client=mock_client())


def test_ground_truth_github_repository_url_fails_before_html_polling() -> None:
    source = OPENAI.sources[0]

    with pytest.raises(ValueError, match="direct payload URL"):
        OpenAIProviderAdapter(source, client=mock_client())


def test_direct_github_raw_url_is_allowed() -> None:
    source = ProviderSource(
        "openapi",
        "https://raw.githubusercontent.com/openai/openapi/main/openapi.json",
        True,
    )
    provider = Provider("test", "Test", "test", "https://example.test/test", (source,))

    adapter = ConfiguredProviderAdapter(provider, source, client=mock_client())

    assert adapter.provider == "test"
