from __future__ import annotations

import orjson
import pytest

from Driftwatch.Watch.PayloadIndexers import (
    OpenApiPayloadIndexer,
    PayloadIndexingError,
    RazorpaySdkPayloadIndexer,
)


def test_openai_openapi_payload_is_indexed() -> None:
    raw = orjson.dumps(
        {
            "openapi": "3.0.0",
            "paths": {
                "/v1/responses": {
                    "post": {
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )

    result = OpenApiPayloadIndexer().index(raw, revision="test")

    assert result.content_sha256
    assert "api:openai/op:post_v1_responses" in result.index.entries


def test_openai_indexer_rejects_non_openapi_json() -> None:
    with pytest.raises(PayloadIndexingError, match="OpenAPI"):
        OpenApiPayloadIndexer().index(b'{"items":[]}', revision="test")


def test_razorpay_source_indexes_only_provable_operations() -> None:
    raw = b"""
def create_payment(self, payload):
    return self.request('/v1/payments', method='POST', data=payload)

def unrelated(self):
    return '/v1/not-an-operation'
"""

    result = RazorpaySdkPayloadIndexer().index(raw, revision="commit")

    assert "api:razorpay/op:post_v1_payments" in result.index.entries
    assert len(result.index.entries) == 1


def test_razorpay_indexer_quarantines_invalid_or_empty_source() -> None:
    with pytest.raises(PayloadIndexingError, match="valid Python"):
        RazorpaySdkPayloadIndexer().index(b"not valid: [", revision="test")
    with pytest.raises(PayloadIndexingError, match="no provable"):
        RazorpaySdkPayloadIndexer().index(b"def f(): return '/v1/payments'", revision="test")
