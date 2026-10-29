"""Raw HTTP usage with no SDK at all, which is common for Razorpay-style integrations."""

import os

import requests


def charge_via_http(token, amount):
    response = requests.post(
        "https://api.stripe.com/v1/charges",
        auth=(os.environ["STRIPE_KEY"], ""),
        data={"amount": amount, "currency": "usd", "source": token},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()
