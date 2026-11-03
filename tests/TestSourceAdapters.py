from __future__ import annotations

from hashlib import sha256

import httpx2

from Driftwatch.Providers import ProviderSource
from Driftwatch.Surfaces import SurfaceDescriptor
from Driftwatch.Watch.Differ import Delta
from Driftwatch.Watch.Evidence import HttpEvidenceRetriever
from Driftwatch.Watch.SourceAdapters import HttpSourceAdapter, SourceState


def test_poll_returns_payload_and_watermark() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["if-none-match"] == "old"
        return httpx2.Response(
            200,
            content=b'{"version": 2}',
            headers={"etag": "new", "last-modified": "today"},
        )

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = HttpSourceAdapter("https://example.test/spec.json", client=client).poll(
        SourceState(etag="old", content_hash="old-hash")
    )

    assert result.changed is True
    assert result.payload == b'{"version": 2}'
    assert result.state.etag == "new"


def test_poll_suppresses_identical_content_without_http_validator() -> None:
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=b"same"))
    )
    result = HttpSourceAdapter("https://example.test/spec.json", client=client).poll(
        SourceState(content_hash=sha256(b"same").hexdigest())
    )

    assert result.changed is False
    assert result.payload is None


def test_evidence_retriever_returns_verbatim_provider_quote() -> None:
    source = ProviderSource("changelog_html", "https://example.test/changelog", False)
    client = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200,
                text="Intro\nPOST /v1/charges requires source\nFooter",
            )
        )
    )
    delta = Delta(
        surface_ref="api:stripe/op:post_v1_charges",
        change_type="param_added_required",
        severity="breaking",
        op="modified",
        before=SurfaceDescriptor(kind="op", type_sig="POST /v1/charges"),
        after=None,
        detail="source is now required",
    )

    evidence = HttpEvidenceRetriever((source,), client=client).retrieve(delta)

    assert evidence[0].kind == "raw_delta"
    provider_evidence = evidence[1]
    assert provider_evidence.quote == "Intro\nPOST /v1/charges requires source\nFooter"
    assert provider_evidence.source_url == source.url
    assert provider_evidence.locator == "line 2"


def test_validate_egress_url_rejects_non_https_and_loopback() -> None:
    import pytest

    from Driftwatch.Watch.SourceAdapters import validate_egress_url

    with pytest.raises(ValueError, match="only HTTPS"):
        validate_egress_url("http://example.com/spec.json")

    with pytest.raises(ValueError, match="blocked non-public IP"):
        validate_egress_url("https://127.0.0.1/spec.json")


def test_poll_rejects_oversized_stream() -> None:
    import pytest

    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=b"x" * 200))
    )
    adapter = HttpSourceAdapter("https://example.test/spec.json", client=client, max_bytes=100)

    with pytest.raises(ValueError, match="exceeds 100 bytes"):
        adapter.poll(SourceState())
