"""Auth0 token validation and authenticated request principals."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt


class AuthenticationError(ValueError):
    """Raised when an access token cannot be trusted."""


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Identity and tenant claims extracted from a verified Auth0 token."""

    subject: str
    account_id: str
    scopes: frozenset[str]


class JwksClient(Protocol):
    """Fetch signing keys for the configured Auth0 issuer."""

    def get_signing_key(self, token: str) -> Any:
        """Return the key selected by the token's ``kid`` header."""


class Auth0JwksClient:
    """Small adapter around PyJWT's rotating JWKS client."""

    def __init__(self, issuer: str, *, timeout: int = 5) -> None:
        self._client = jwt.PyJWKClient(
            f"{issuer.rstrip('/')}/.well-known/jwks.json",
            cache_keys=True,
            max_cached_keys=16,
            timeout=timeout,
        )

    def get_signing_key(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


def verify_shared_secret(expected: str, provided: str) -> bool:
    """Constant-time secret comparison that rejects empty secrets."""
    if not expected or not expected.strip() or not provided or not provided.strip():
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


class Auth0TokenValidator:
    """Validate Auth0 RS256 access tokens with exact issuer and audience checks."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        *,
        account_claim: str = "https://driftwatch.dev/account_id",
        jwks_client: JwksClient | None = None,
    ) -> None:
        normalized_issuer = issuer.rstrip("/") + "/"
        if not normalized_issuer.startswith("https://"):
            raise ValueError("Auth0 issuer must use HTTPS")
        if not audience.strip():
            raise ValueError("Auth0 audience must not be empty")
        self._issuer = normalized_issuer
        self._audience = audience
        self._account_claim = account_claim
        self._jwks = jwks_client or Auth0JwksClient(normalized_issuer)

    def validate(self, token: str) -> AuthenticatedPrincipal:
        """Verify a bearer token and return only trusted identity claims."""
        if not token.strip():
            raise AuthenticationError("access token must not be empty")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise AuthenticationError("unsupported token algorithm")
            claims = jwt.decode(
                token,
                self._jwks.get_signing_key(token),
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except AuthenticationError:
            raise
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, OSError) as error:
            raise AuthenticationError("invalid access token") from error

        subject = _required_string(claims, "sub")
        account_id = _required_string(claims, self._account_claim)
        scopes = frozenset(_scopes(claims.get("scope")))
        return AuthenticatedPrincipal(subject, account_id, scopes)


def _required_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError(f"required token claim missing: {name}")
    return value


def _scopes(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise AuthenticationError("token scope claim must be a string")
    return tuple(scope for scope in value.split() if scope)
