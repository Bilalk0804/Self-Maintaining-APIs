from __future__ import annotations

from hashlib import sha256

import httpx

from Driftwatch.Watch.SourceAdapters import HttpSourceAdapter, SourceState


def test_poll_returns_payload_and_watermark() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == "old"
        return httpx.Response(
            200,
            content=b'{"version": 2}',
            headers={"etag": "new", "last-modified": "today"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = HttpSourceAdapter("https://example.test/spec.json", client=client).poll(
        SourceState(etag="old", content_hash="old-hash")
    )

    assert result.changed is True
    assert result.payload == b'{"version": 2}'
    assert result.state.etag == "new"


def test_poll_suppresses_identical_content_without_http_validator() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"same"))
    )
    result = HttpSourceAdapter("https://example.test/spec.json", client=client).poll(
        SourceState(content_hash=sha256(b"same").hexdigest())
    )

    assert result.changed is False
    assert result.payload is None
