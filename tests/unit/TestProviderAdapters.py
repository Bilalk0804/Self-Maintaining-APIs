from __future__ import annotations

import httpx
import pytest

from Driftwatch.Providers import OPENAI, RAZORPAY, ProviderSource
from Driftwatch.Watch.ProviderAdapters import (
    OpenAIProviderAdapter,
    RazorpayProviderAdapter,
    provider_adapter,
)
from Driftwatch.Watch.SourceAdapters import SourceState


def mock_client(payload: bytes = b"source") -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    )


def test_openai_adapter_polls_configured_source() -> None:
    source = OPENAI.sources[0]
    adapter = OpenAIProviderAdapter(source, client=mock_client(b"openapi"))

    result = adapter.poll(SourceState())

    assert adapter.provider == "openai"
    assert adapter.source.kind == "openapi"
    assert result.changed is True
    assert result.payload == b"openapi"


def test_razorpay_adapter_polls_sdk_source() -> None:
    source = RAZORPAY.sources[0]
    adapter = RazorpayProviderAdapter(source, client=mock_client(b"sdk"))

    assert adapter.poll(SourceState()).payload == b"sdk"


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
