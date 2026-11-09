"""End-to-end tests for the PayPal AI Hackathon showcase."""

from __future__ import annotations

import json
from pathlib import Path

from Driftwatch.Services.PayPalShowcase import (
    GroundedRuleModelClient,
    build_evidence_for_delta,
    run_paypal_showcase,
)
from Driftwatch.Surfaces import SurfaceDescriptor
from Driftwatch.Watch.Classifier import EvidenceGroundedClassifier
from Driftwatch.Watch.Differ import Delta

_CONTROLLER = """
class OrdersController:
    def create_order(self, options=dict()):
        return (
            RequestBuilder().path("/v2/checkout/orders")
            .http_method(HttpMethodEnum.POST)
            .body_param(Parameter().value(options.get("body", None)))
        )

    def confirm_order(self, options=dict()):
        return (
            RequestBuilder().path("/v2/checkout/orders/{id}/confirm-payment-source")
            .http_method(HttpMethodEnum.POST)
            .body_param(Parameter().value(options.get("body", None)))
        )

    def create_order_tracking(self, options=dict()):
        return (
            RequestBuilder().path("/v2/checkout/orders/{id}/track")
            .http_method(HttpMethodEnum.POST)
            .body_param(Parameter().value(options.get("body", None)))
        )
"""


def test_grounded_rule_client_passes_evidence_grounding_validator() -> None:
    delta = Delta(
        surface_ref="api:paypal/op:post_v2_checkout_orders/param:payer",
        change_type="param_deprecated",
        severity="deprecating",
        op="modified",
        before=SurfaceDescriptor(kind="param"),
        after=SurfaceDescriptor(kind="param", deprecated=True),
        detail="payer marked deprecated",
    )
    evidence = build_evidence_for_delta(delta)

    record = EvidenceGroundedClassifier(GroundedRuleModelClient()).classify(delta, evidence)

    assert record.change_type == "param_deprecated"
    assert record.severity == "deprecating"
    assert record.evidence[0].quote == "payer marked deprecated"


def test_paypal_showcase_runs_end_to_end_with_fixture_repo(tmp_path: Path) -> None:
    cache_dir = _paypal_cache(tmp_path)
    reviews_dir = tmp_path / "reviews"

    report = run_paypal_showcase(
        cache_dir=cache_dir,
        repo_path=Path("tests/fixtures/paypalRepo"),
        reviews_dir=reviews_dir,
        client_id="your sandbox client id",
        client_secret="your sandbox secret",
        openai_api_key="",
    )

    assert report.sdk_operations >= 3
    assert report.operation_deltas >= 4
    assert len(report.classifications) == report.operation_deltas
    assert len(report.findings) >= 4
    assert (reviews_dir / "paypal-findings.json").is_file()
    assert report.sandbox_checkout.amount == "49.00"


def _paypal_cache(root: Path) -> Path:
    """Write a tiny PayPal SDK and two OpenAPI revisions under ``root``."""
    cache = root / "cache"
    controllers = cache / "PayPal-Python-Server-SDK" / "paypalserversdk" / "controllers"
    controllers.mkdir(parents=True)
    (controllers / "orders_controller.py").write_text(_CONTROLLER, encoding="utf-8")
    (cache / "paypal_before.json").write_text(json.dumps(_spec(after=False)), encoding="utf-8")
    (cache / "paypal_after.json").write_text(json.dumps(_spec(after=True)), encoding="utf-8")
    return cache


def _spec(*, after: bool) -> dict[str, object]:
    payer: dict[str, object] = {"type": "object"}
    if after:
        payer["deprecated"] = True
        payer["type"] = "string"
    confirm_props: dict[str, object] = {
        "payment_source": {"type": "object"},
    }
    if not after:
        confirm_props["processing_instruction"] = {"type": "string"}
    track_required = ["carrier", "tracking_number"] if after else ["capture_id"]
    carrier_enum = ["FEDEX", "SHIPBOB"] if after else ["FEDEX"]
    return {
        "openapi": "3.0.3",
        "info": {"title": "PayPal Checkout", "version": "2"},
        "paths": {
            "/v2/checkout/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "intent": {"type": "string"},
                                        "payer": payer,
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/v2/checkout/orders/{id}/confirm-payment-source": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": confirm_props,
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/v2/checkout/orders/{id}/track": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": track_required,
                                    "properties": {
                                        "capture_id": {"type": "string"},
                                        "carrier": {"type": "string", "enum": carrier_enum},
                                        "tracking_number": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
