from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from Driftwatch.Controllers.Auth import AuthenticatedPrincipal
from Driftwatch.Controllers.ScanController import ScanResponse
from Driftwatch.Http.Application import create_app
from Driftwatch.Services.Growth import ImpactSummary

PRINCIPAL = AuthenticatedPrincipal("auth0|user-1", "account-1", frozenset())


class TokenValidator:
    def validate(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid":
            raise ValueError("invalid")
        return PRINCIPAL


class Controller:
    def __init__(self, account_id: str = "account-1") -> None:
        self.account_id = account_id
        self.requests: list[tuple] = []

    def submit_scan(self, principal: AuthenticatedPrincipal, request: object) -> ScanResponse:
        self.requests.append((principal, request))
        return ScanResponse(
            UUID("00000000-0000-0000-0000-000000000001"),
            "accepted",
            "scan:abc",
        )

    def dashboard(self, principal: AuthenticatedPrincipal) -> list[ImpactSummary]:
        return [
            ImpactSummary(
                "change-1",
                "stripe",
                "param_renamed",
                "breaking",
                "Rename source",
                None,
                None,
                1,
                1,
                0.9,
            )
        ]


def test_health_is_public_and_ready_can_be_checked() -> None:
    client = TestClient(create_app(Controller(), TokenValidator(), readiness_check=lambda: True))

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_scan_requires_bearer_authentication() -> None:
    client = TestClient(create_app(Controller(), TokenValidator()))

    response = client.post(
        "/v1/scans",
        json={"repository_id": "repo-1", "provider": "stripe", "commit_sha": "abc123"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_scan_passes_only_public_fields_and_verified_principal() -> None:
    controller = Controller()
    client = TestClient(create_app(controller, TokenValidator()))

    response = client.post(
        "/v1/scans",
        headers={"Authorization": "Bearer valid"},
        json={
            "repository_id": "repo-1",
            "provider": "stripe",
            "commit_sha": "abc123",
            "account_id": "attacker-account",
            "repository_path": "/etc",
        },
    )

    assert response.status_code == 422
    assert controller.requests == []


def test_dashboard_uses_verified_principal() -> None:
    client = TestClient(create_app(Controller(), TokenValidator()))

    response = client.get("/v1/dashboard/impact", headers={"Authorization": "Bearer valid"})

    assert response.status_code == 200
    assert response.json()[0]["provider"] == "stripe"


def test_unready_dependency_returns_service_unavailable() -> None:
    client = TestClient(create_app(Controller(), TokenValidator(), readiness_check=lambda: False))

    assert client.get("/readyz").status_code == 503
