from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from Driftwatch.Controllers.Auth import AuthenticatedPrincipal
from Driftwatch.Controllers.ScanController import (
    AuthorizedRepository,
    PostgresRepositoryAuthorizer,
    ScanController,
    ScanRequest,
)


class Queue:
    def __init__(self, job_id: UUID | None) -> None:
        self.job_id = job_id
        self.calls: list[tuple] = []

    def enqueue(self, *args: object, **kwargs: object) -> UUID | None:
        self.calls.append((args, kwargs))
        return self.job_id


class Dashboard:
    def upcoming_impact(self, account_id: str) -> list:
        return [account_id]


class Repositories:
    def __init__(self, account_id: str = "account-1") -> None:
        self.account_id = account_id

    def resolve(
        self, principal: AuthenticatedPrincipal, repository_id: str
    ) -> AuthorizedRepository:
        return AuthorizedRepository(repository_id, self.account_id, Path("/srv/repos/app"))


PRINCIPAL = AuthenticatedPrincipal("auth0|user-1", "account-1", frozenset())


def request() -> ScanRequest:
    return ScanRequest("repo-1", "stripe", "abc123")


def controller(queue: Queue, repositories: Repositories | None = None) -> ScanController:
    return ScanController(queue, Dashboard(), repositories or Repositories())


def test_controller_enqueues_authorized_scan_and_returns_accepted() -> None:
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    queue = Queue(job_id)

    response = controller(queue).submit_scan(PRINCIPAL, request())

    assert response.job_id == job_id
    assert response.status == "accepted"
    assert response.idempotency_key.startswith("scan:")
    payload = queue.calls[0][0][2]
    assert payload["account_id"] == "account-1"
    assert payload["repository_path"] == "/srv/repos/app"


def test_controller_reports_existing_idempotent_job() -> None:
    response = controller(Queue(None)).submit_scan(PRINCIPAL, request())

    assert response.status == "already_queued"


def test_controller_rejects_repository_owned_by_another_account() -> None:
    with pytest.raises(PermissionError, match="not owned"):
        controller(Queue(None), Repositories("account-2")).submit_scan(PRINCIPAL, request())


def test_controller_rejects_unknown_provider() -> None:
    invalid = ScanRequest("repo", "unknown", "sha")

    with pytest.raises(ValueError, match="unsupported provider"):
        controller(Queue(None)).submit_scan(PRINCIPAL, invalid)


def test_controller_delegates_dashboard_using_verified_account() -> None:
    assert controller(Queue(None)).dashboard(PRINCIPAL) == ["account-1"]


def test_repository_authorizer_derives_workspace_from_server_root(tmp_path: Path) -> None:
    class Connection:
        def execute(self, query: str, params: tuple) -> object:
            assert "account_id = %s" in query
            assert params[1] == "account-1"
            return type("Result", (), {"fetchone": lambda self: ("account-1",)})()

    repository_id = "00000000-0000-0000-0000-000000000001"
    authorized = PostgresRepositoryAuthorizer(Connection(), tmp_path).resolve(
        PRINCIPAL, repository_id
    )

    assert authorized.workspace == (tmp_path / repository_id).resolve()


def test_repository_authorizer_rejects_missing_or_other_tenant_repository(
    tmp_path: Path,
) -> None:
    class Connection:
        def execute(self, query: str, params: tuple) -> object:
            return type("Result", (), {"fetchone": lambda self: None})()

    authorizer = PostgresRepositoryAuthorizer(Connection(), tmp_path)

    with pytest.raises(PermissionError, match="not accessible"):
        authorizer.resolve(PRINCIPAL, "00000000-0000-0000-0000-000000000001")
