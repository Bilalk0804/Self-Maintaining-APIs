"""Regression coverage for Stripe's current unified OpenAPI shape."""

from __future__ import annotations

from Driftwatch.Watch.Differ import diff_indexes
from Driftwatch.Watch.OpenapiIndexer import build_index


def _document(*, include_payment_method_types: bool, ui_modes: list[str]) -> dict:
    properties = {
        "ui_mode": {
            "type": "string",
            "enum": ui_modes,
        }
    }
    if include_payment_method_types:
        properties["payment_method_types"] = {
            "type": "array",
            "items": {"type": "string"},
        }

    return {
        "openapi": "3.1.0",
        "paths": {
            "/v1/checkout/sessions": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/x-www-form-urlencoded": {
                                "schema": {
                                    "type": "object",
                                    "properties": properties,
                                }
                            }
                        }
                    }
                }
            }
        },
        "webhooks": {
            "payment_intent.succeeded": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["id"],
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        }
                    }
                }
            }
        },
    }


def test_indexes_webhooks_in_unified_openapi_documents() -> None:
    index = build_index(
        _document(include_payment_method_types=True, ui_modes=["hosted", "embedded"]),
        "stripe",
        "before",
    )

    assert "api:stripe/event:payment_intent.succeeded" in index.entries
    assert "api:stripe/event:payment_intent.succeeded/param:id" in index.entries


def test_detects_endive_parameter_removal_and_dahlia_enum_removals() -> None:
    before = build_index(
        _document(include_payment_method_types=True, ui_modes=["hosted", "embedded"]),
        "stripe",
        "before",
    )
    after = build_index(
        _document(include_payment_method_types=False, ui_modes=["hosted_page", "embedded_page"]),
        "stripe",
        "after",
    )

    deltas = diff_indexes(before, after)
    assert any(
        delta.change_type == "param_removed"
        and delta.surface_ref.endswith("/param:payment_method_types")
        for delta in deltas
    )
    ui_mode_changes = {
        delta.change_type for delta in deltas if delta.surface_ref.endswith("/param:ui_mode")
    }
    assert "enum_value_removed" in ui_mode_changes
