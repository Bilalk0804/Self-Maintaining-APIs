"""Near-miss code that a text-only matcher flags and an AST matcher must not.

Every symbol here is deliberately named to collide with a Stripe surface. None of it touches
Stripe. A matcher that reports any finding in this file is unsafe to put behind a pull
request, because these shapes are far more common in real repositories than the true
positives are.
"""

from internal.billing import InternalBilling
from internal.mail import send_email

billing = InternalBilling()


def notify(customer_email):
    # Decoy 1: a `source=` keyword argument on a function that is not a Stripe operation.
    send_email(to=customer_email, source="noreply@example.test", subject="Receipt")


def internal_charge(token, amount):
    # Decoy 2: an object whose attribute path mimics `client.charges.create` exactly, but
    # whose root is an in-house class rather than the Stripe SDK.
    return billing.charges.create(amount=amount, source=token)


def summarise(text):
    # Decoy 3: a deprecated model id mentioned only in a comment, not in code.
    # Historically we used "gpt-4-vision-preview" here before migrating away.
    return text.upper()


def read_local_charge(row):
    # Decoy 4: a local variable named `charge` with a `.source` attribute, unrelated to any
    # Stripe response object.
    charge = {"source": row.get("origin")}
    return charge["source"]


def payment_method_types():
    # Decoy 5: a function whose *name* matches a renamed Stripe parameter.
    return ["card", "upi"]
