from __future__ import annotations

from uuid import uuid4

from Driftwatch.Services.PayPalDemo import PayPalSourceResult


def test_paypal_source_result_is_immutable() -> None:
    result = PayPalSourceResult("sdk_source", uuid4(), "built", 41)

    assert result.kind == "sdk_source"
    assert result.indexed_entries == 41
