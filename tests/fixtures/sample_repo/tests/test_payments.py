"""Minimal test suite, present so test-runner detection has something to find."""

from app.decoys import payment_method_types


def test_payment_method_types():
    assert "card" in payment_method_types()
