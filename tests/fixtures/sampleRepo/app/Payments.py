"""Client-style Stripe usage: the modern StripeClient surface."""

import os

import stripe

client = stripe.StripeClient(os.environ["STRIPE_KEY"])


def charge_customer(token, amount):
    charge = client.charges.create(
        amount=amount,
        currency="usd",
        source=token,
        description="order payment",
    )
    # Response attribute read on a value returned by a matched call.
    if charge.source is not None:
        print("paid with", charge.source)
    return charge.id


def capture_later(charge_id, amount):
    return client.charges.capture(charge_id, amount=amount)
