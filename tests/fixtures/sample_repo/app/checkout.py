"""Checkout session creation, using the parameter Stripe actually renamed."""

import stripe

client = stripe.StripeClient("sk_test_x")


def start_checkout(price_id):
    session = client.checkout.sessions.create(
        mode="payment",
        payment_method_types=["card", "link"],
        line_items=[{"price": price_id, "quantity": 1}],
        success_url="https://example.test/ok",
    )
    return session.url
