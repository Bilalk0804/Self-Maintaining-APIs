"""PayPal sandbox checkout for a single Driftwatch workspace plan.

The host is fixed to ``api-m.sandbox.paypal.com``. A caller cannot point this
client at the live PayPal API, and the buyer cannot choose the price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx2

SANDBOX_ORIGIN = "https://api-m.sandbox.paypal.com"
PLAN_NAME = "Driftwatch workspace"
PLAN_AMOUNT = "49.00"
PLAN_CURRENCY = "USD"
_ORDER_ID = re.compile(r"^[A-Z0-9]{10,20}$")


class PayPalCheckoutError(RuntimeError):
    """The PayPal sandbox rejected a checkout call or returned an unusable body."""


@dataclass(frozen=True, slots=True)
class PayPalCheckoutConfig:
    """Sandbox REST app credentials. Both values come from developer.paypal.com."""

    client_id: str
    client_secret: str
    return_url: str = "https://example.com/driftwatch/paid"
    cancel_url: str = "https://example.com/driftwatch/cancelled"
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.client_id.strip() or not self.client_secret.strip():
            raise ValueError("PayPal sandbox client id and secret are required")
        if not self.return_url.startswith("https://") or not self.cancel_url.startswith("https://"):
            raise ValueError("PayPal return URLs must use HTTPS")
        if self.timeout_seconds <= 0:
            raise ValueError("PayPal timeout must be positive")


@dataclass(frozen=True, slots=True)
class CheckoutOrder:
    """A created sandbox order waiting for the buyer to approve it."""

    order_id: str
    approve_url: str
    plan: str
    amount: str
    currency: str


@dataclass(frozen=True, slots=True)
class CapturedPayment:
    """A sandbox capture of the workspace plan."""

    order_id: str
    capture_id: str
    status: str
    amount: str
    currency: str


class PayPalCheckout:
    """Create and capture the fixed workspace plan in the PayPal sandbox."""

    def __init__(
        self,
        config: PayPalCheckoutConfig,
        *,
        client: httpx2.Client | None = None,
    ) -> None:
        if client is not None and str(client.base_url).rstrip("/") != SANDBOX_ORIGIN:
            raise ValueError("PayPal checkout is sandbox-only")
        self._config = config
        self._client = client or httpx2.Client(
            base_url=SANDBOX_ORIGIN,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def close(self) -> None:
        """Close the HTTP client when this service created it."""
        if self._owns_client:
            self._client.close()

    def create_order(self) -> CheckoutOrder:
        """Create a sandbox order for the workspace plan and return its approval link.

        Returns:
            The order id and the URL a sandbox buyer opens to approve payment.

        Raises:
            PayPalCheckoutError: The sandbox token or order call failed.
        """
        token = self._access_token()
        response = self._client.post(
            "/v2/checkout/orders",
            headers=_json_headers(token),
            json={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "custom_id": "driftwatch-workspace",
                        "description": f"{PLAN_NAME}, monthly",
                        "amount": {"currency_code": PLAN_CURRENCY, "value": PLAN_AMOUNT},
                    }
                ],
                "application_context": {
                    "brand_name": "Driftwatch",
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING",
                    "return_url": self._config.return_url,
                    "cancel_url": self._config.cancel_url,
                },
            },
        )
        _raise_for_status(response)
        body = _json_object(response)
        order_id = body.get("id")
        approve = _approve_url(body.get("links"))
        order_ok = isinstance(order_id, str) and _ORDER_ID.fullmatch(order_id) is not None
        if not order_ok or approve is None:
            raise PayPalCheckoutError("PayPal sandbox order response is missing an approval link")
        return CheckoutOrder(order_id, approve, PLAN_NAME, PLAN_AMOUNT, PLAN_CURRENCY)

    def capture(self, order_id: str) -> CapturedPayment:
        """Capture an approved sandbox order.

        Args:
            order_id: PayPal order id returned from create, also the ``token`` on the return URL.

        Returns:
            The capture id and status reported by the sandbox.

        Raises:
            ValueError: ``order_id`` is not a PayPal order id.
            PayPalCheckoutError: The sandbox rejected the capture.
        """
        if _ORDER_ID.fullmatch(order_id) is None:
            raise ValueError("PayPal order id is not valid")
        token = self._access_token()
        response = self._client.post(
            f"/v2/checkout/orders/{order_id}/capture",
            headers=_json_headers(token),
        )
        _raise_for_status(response)
        body = _json_object(response)
        capture = _capture(body)
        if capture is None:
            raise PayPalCheckoutError("PayPal sandbox capture response has no capture")
        capture_id, status, amount, currency = capture
        return CapturedPayment(order_id, capture_id, status, amount, currency)

    def _access_token(self) -> str:
        response = self._client.post(
            "/v1/oauth2/token",
            auth=(self._config.client_id, self._config.client_secret),
            headers={"Accept": "application/json"},
            data={"grant_type": "client_credentials"},
        )
        _raise_for_status(response)
        token = _json_object(response).get("access_token")
        if not isinstance(token, str) or not token:
            raise PayPalCheckoutError("PayPal sandbox did not return an access token")
        return token


def _json_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _raise_for_status(response: httpx2.Response) -> None:
    if response.status_code < 400:
        return
    detail = f"PayPal sandbox returned {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        name = payload.get("name") or payload.get("error")
        if isinstance(name, str) and name:
            detail = f"{detail}: {name}"
    raise PayPalCheckoutError(detail)


def _json_object(response: httpx2.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise PayPalCheckoutError("PayPal sandbox returned a non-JSON body") from error
    if not isinstance(payload, dict):
        raise PayPalCheckoutError("PayPal sandbox returned an unexpected body")
    return payload


def _approve_url(links: object) -> str | None:
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "approve":
            continue
        href = link.get("href")
        if isinstance(href, str) and href.startswith("https://www.sandbox.paypal.com/"):
            return href
    return None


def _capture(body: dict[str, object]) -> tuple[str, str, str, str] | None:
    units = body.get("purchase_units")
    if not isinstance(units, list) or not units or not isinstance(units[0], dict):
        return None
    payments = units[0].get("payments")
    if not isinstance(payments, dict):
        return None
    captures = payments.get("captures")
    if not isinstance(captures, list) or not captures or not isinstance(captures[0], dict):
        return None
    capture = captures[0]
    capture_id = capture.get("id")
    status = capture.get("status")
    amount = capture.get("amount")
    if (
        not isinstance(capture_id, str)
        or not isinstance(status, str)
        or not isinstance(amount, dict)
    ):
        return None
    value = amount.get("value")
    currency = amount.get("currency_code")
    if not isinstance(value, str) or not isinstance(currency, str):
        return None
    return capture_id, status, value, currency
