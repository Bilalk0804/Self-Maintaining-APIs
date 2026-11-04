from __future__ import annotations

import io
import tarfile

import httpx2
import pytest

from Driftwatch.Providers import OPENAI, RAZORPAY, Provider, ProviderSource
from Driftwatch.Watch.GithubSourceAdapters import GithubRepositorySourceAdapter
from Driftwatch.Watch.ProviderAdapters import (
    ConfiguredProviderAdapter,
    OpenAIProviderAdapter,
    RazorpayProviderAdapter,
    provider_adapter,
)
from Driftwatch.Watch.SourceAdapters import SourceState


def mock_client(payload: bytes = b"source") -> httpx2.Client:
    return httpx2.Client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=payload))
    )


def test_openai_repository_source_uses_archive_adapter() -> None:
    source = OPENAI.sources[0]

    adapter = OpenAIProviderAdapter(source, client=mock_client(b"archive"))

    assert adapter.source == source


def test_razorpay_repository_source_uses_archive_adapter() -> None:
    source = RAZORPAY.sources[0]

    adapter = RazorpayProviderAdapter(source, client=mock_client(b"archive"))

    assert adapter.source == source

def test_factory_selects_source_kind() -> None:
    adapter = provider_adapter("razorpay", "changelog_html", client=mock_client())

    assert adapter.provider == "razorpay"
    assert adapter.source.ground_truth is False


def test_factory_rejects_unconfigured_source_kind() -> None:
    with pytest.raises(ValueError, match="no configured"):
        provider_adapter("razorpay", "openapi", client=mock_client())


def test_factory_rejects_database_url_drift() -> None:
    with pytest.raises(ValueError, match="does not match"):
        provider_adapter(
            "razorpay",
            "changelog_html",
            source_url="https://example.test/not-the-catalog-url",
            client=mock_client(),
        )


def test_adapter_rejects_source_from_another_provider() -> None:
    source = ProviderSource("openapi", "https://example.test/other", True)

    with pytest.raises(ValueError, match="not configured"):
        OpenAIProviderAdapter(source, client=mock_client())


def test_ground_truth_github_repository_url_uses_archive_adapter() -> None:
    source = OPENAI.sources[0]

    adapter = OpenAIProviderAdapter(source, client=mock_client())

    assert adapter.source == source


def test_direct_github_raw_url_is_allowed() -> None:
    source = ProviderSource(
        "openapi",
        "https://raw.githubusercontent.com/openai/openapi/main/openapi.json",
        True,
    )
    provider = Provider("test", "Test", "test", "https://example.test/test", (source,))

    adapter = ConfiguredProviderAdapter(provider, source, client=mock_client())

    assert adapter.provider == "test"


def test_github_sdk_archive_extracts_python_sources() -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        content = b"def create(self):\n return self.request('/v2/orders', method='POST')\n"
        info = tarfile.TarInfo("sdk-main/paypalserversdk/orders.py")
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=archive.getvalue()))
    )
    adapter = GithubRepositorySourceAdapter(
        "https://github.com/paypal/PayPal-Python-Server-SDK",
        "sdk_source",
        client=client,
    )

    result = adapter.poll(SourceState())

    assert result.changed is True
    assert b"/v2/orders" in (result.payload or b"")


def test_github_openapi_archive_merges_json_documents() -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for path in ("orders.json", "payments.json"):
            content = (
                b'{"openapi":"3.0.0","paths":{"/v2/%s":{"get":{"responses":{}}}}}'
                % path.split(".")[0].encode()
            )
            info = tarfile.TarInfo(f"spec-main/openapi/{path}")
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=archive.getvalue()))
    )
    adapter = GithubRepositorySourceAdapter(
        "https://github.com/paypal/paypal-rest-api-specifications",
        "openapi",
        client=client,
    )

    result = adapter.poll(SourceState())

    assert result.changed is True
    assert b"/v2/orders" in (result.payload or b"")
    assert b"/v2/payments" in (result.payload or b"")
