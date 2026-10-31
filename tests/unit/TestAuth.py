from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from Driftwatch.Controllers.Auth import (
    Auth0TokenValidator,
    AuthenticationError,
)


class Jwks:
    def __init__(self, key: str) -> None:
        self.key = key

    def get_signing_key(self, token: str) -> str:
        return self.key


def test_validator_requires_https_issuer() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        Auth0TokenValidator("http://tenant.example/", "api", jwks_client=Jwks("secret"))


def test_validator_rejects_non_rs256_tokens() -> None:
    token = jwt.encode({"alg": "HS256"}, "a" * 32, algorithm="HS256")
    validator = Auth0TokenValidator("https://tenant.example/", "api", jwks_client=Jwks("secret"))

    with pytest.raises(AuthenticationError, match="algorithm"):
        validator.validate(token)


def test_validator_returns_verified_principal() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "iss": "https://tenant.example/",
            "aud": "api",
            "sub": "auth0|user-1",
            "exp": 4_000_000_000,
            "iat": 1_700_000_000,
            "https://driftwatch.dev/account_id": "account-1",
            "scope": "scan:write dashboard:read",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    validator = Auth0TokenValidator(
        "https://tenant.example",
        "api",
        jwks_client=Jwks(private_key.public_key()),
    )

    principal = validator.validate(token)

    assert principal.subject == "auth0|user-1"
    assert principal.account_id == "account-1"
    assert principal.scopes == frozenset({"scan:write", "dashboard:read"})
