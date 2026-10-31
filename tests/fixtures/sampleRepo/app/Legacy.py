"""Legacy static-resource style, reached through an aliased import."""

import stripe as pay

pay.api_key = "sk_test_x"
pay.api_version = "2024-06-20"


def legacy_charge(token):
    return pay.Charge.create(amount=500, currency="usd", source=token)
