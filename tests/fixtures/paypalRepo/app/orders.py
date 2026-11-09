"""Sample PayPal merchant repository affected by real Orders v2 API drift."""

from __future__ import annotations

from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient


def create_checkout_order(email: str) -> object:
    """Create a PayPal order using the deprecated payer object."""
    client = PaypalServersdkClient()
    return client.orders.create_order(
        body={
            "intent": "CAPTURE",
            "payer": {"email_address": email},
            "purchase_units": [{"amount": {"currency_code": "USD", "value": "49.00"}}],
        }
    )


def confirm_payment(order_id: str) -> object:
    """Confirm order payment using the removed processing_instruction parameter."""
    client = PaypalServersdkClient()
    return client.orders.confirm_order(
        id=order_id,
        body={
            "processing_instruction": "ORDER_COMPLETE_ON_PAYMENT_APPROVAL",
            "payment_source": {"paypal": {"name": {"given_name": "Ada", "surname": "Lovelace"}}},
        },
    )


def track_shipment(order_id: str) -> object:
    """Add shipment tracking affected by carrier and tracking_number becoming required."""
    client = PaypalServersdkClient()
    return client.orders.create_order_tracking(
        id=order_id,
        body={
            "capture_id": "CAP-100",
            "carrier": "SHIPBOB",
            "tracking_number": "1Z999AA10123456784",
        },
    )
