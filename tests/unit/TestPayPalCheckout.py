"""The workspace plan can be sold only through the PayPal sandbox."""

from __future__ import annotations

import json

import httpx2
import pytest
from fastapi.testclient import TestClient

from Driftwatch.Http.Application import create_app
from Driftwatch.Services.PayPalCheckout import (
    PLAN_AMOUNT,
    SANDBOX_ORIGIN,
    CapturedPayment,
    CheckoutOrder,
    PayPalCheckout,
    PayPalCheckoutConfig,
    PayPalCheckoutError,
)
from tests.unit.TestHttpApplication import Controller, TokenValidator


def test_checkout_creates_a_fixed_sandbox_order() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx2.Response(200, json={"access_token": "sandbox-token"})
        return httpx2.Response(
            201,
            json={
                "id": "5O190127TN364715T",
                "links": [
                    {
                        "rel": "approve",
                        "href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T",
                    }
                ],
            },
        )

    order = _checkout(handler).create_order()

    assert order.order_id == "5O190127TN364715T"
    assert order.amount == PLAN_AMOUNT
    assert order.approve_url.startswith("https://www.sandbox.paypal.com/")
    assert str(requests[0].url).startswith(SANDBOX_ORIGIN)
    body = json.loads(requests[1].content)
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "49.00"}
    assert requests[1].headers["authorization"] == "Bearer sandbox-token"


def test_checkout_rejects_a_live_paypal_client() -> None:
    with pytest.raises(ValueError, match="sandbox-only"):
        PayPalCheckout(
            PayPalCheckoutConfig("id", "secret"),
            client=httpx2.Client(base_url="https://api-m.paypal.com"),
        )


def test_capture_reads_the_sandbox_capture() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/capture"):
            return httpx2.Response(
                201,
                json={
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": "CAP12345678901234",
                                        "status": "COMPLETED",
                                        "amount": {"currency_code": "USD", "value": "49.00"},
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx2.Response(200, json={"access_token": "sandbox-token"})

    payment = _checkout(handler).capture("5O190127TN364715T")

    assert payment.capture_id == "CAP12345678901234"
    assert payment.status == "COMPLETED"
    assert payment.amount == "49.00"


def test_capture_rejects_an_order_id_before_calling_paypal() -> None:
    called = False

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal called
        called = True
        return httpx2.Response(500)

    with pytest.raises(ValueError, match="order id"):
        _checkout(handler).capture("../live")
    assert called is False


def test_sandbox_error_name_is_reported_without_the_body() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": "invalid_client"})

    with pytest.raises(PayPalCheckoutError, match="401: invalid_client"):
        _checkout(handler).create_order()


def test_billing_route_is_unavailable_until_sandbox_credentials_exist() -> None:
    client = _api(None)

    response = client.post("/v1/billing/checkout")

    assert response.status_code == 503


def test_billing_route_returns_the_sandbox_approval_link() -> None:
    class Checkout:
        def create_order(self) -> CheckoutOrder:
            return CheckoutOrder(
                "5O190127TN364715T",
                "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T",
                "Driftwatch workspace",
                "49.00",
                "USD",
            )

        def capture(self, order_id: str) -> CapturedPayment:
            return CapturedPayment(order_id, "CAP12345678901234", "COMPLETED", "49.00", "USD")

    client = _api(Checkout())

    created = client.post("/v1/billing/checkout")
    captured = client.post("/v1/billing/orders/5O190127TN364715T/capture")

    assert created.status_code == 200
    assert created.json()["approve_url"].startswith("https://www.sandbox.paypal.com/")
    assert captured.json()["status"] == "COMPLETED"


def _checkout(handler: object) -> PayPalCheckout:
    return PayPalCheckout(
        PayPalCheckoutConfig("client-id", "client-secret"),
        client=httpx2.Client(base_url=SANDBOX_ORIGIN, transport=httpx2.MockTransport(handler)),
    )


def _api(checkout: object | None) -> TestClient:
    return TestClient(create_app(Controller(), TokenValidator(), checkout=checkout))
