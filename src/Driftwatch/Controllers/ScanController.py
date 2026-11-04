"""Framework-neutral scan and dashboard controller draft."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from Driftwatch.Controllers.Auth import AuthenticatedPrincipal
from Driftwatch.Providers import get_provider
from Driftwatch.Services.Growth import ImpactSummary
from Driftwatch.Services.JobQueue import JobQueue

_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{6,40}|[A-Za-z0-9_.-]{1,64})$")
_REPO_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F-]{36}|[A-Za-z0-9_.-]{1,100}(?:/[A-Za-z0-9_.-]{1,100})?)$"
)
_ALLOWED_TRIGGERS = frozenset({"scheduled", "new_change", "push", "manual"})


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """Validated request received from an HTTP or CLI client."""

    repository_id: str
    provider: str
    commit_sha: str
    trigger: str = "manual"


@dataclass(frozen=True, slots=True)
class ScanResponse:
    """Accepted scan response returned to a client."""

    job_id: UUID | None
    status: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AuthorizedRepository:
    """Repository identity and workspace resolved after tenant authorization."""

    repository_id: str
    account_id: str
    workspace: Path


class RepositoryAuthorizer(Protocol):
    """Resolve repositories only when they belong to the authenticated account."""

    def resolve(
        self, principal: AuthenticatedPrincipal, repository_id: str
    ) -> AuthorizedRepository:
        """Return an authorized repository or raise ``PermissionError``."""


class RepositoryConnection(Protocol):
    """Minimal database surface used by repository authorization."""

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        """Execute a parameterized repository lookup."""


class PostgresRepositoryAuthorizer:
    """Authorize active repositories and derive workspaces from a trusted root."""

    def __init__(self, connection: RepositoryConnection, workspace_root: Path) -> None:
        self._connection = connection
        self._workspace_root = workspace_root.resolve()

    def resolve(
        self, principal: AuthenticatedPrincipal, repository_id: str
    ) -> AuthorizedRepository:
        """Resolve a tenant-owned repository without accepting a client path."""
        try:
            repository_uuid = UUID(repository_id)
        except ValueError as error:
            raise PermissionError("repository is not accessible") from error
        row = self._connection.execute(
            """
                SELECT account_id
                FROM repo
                WHERE id = %s AND account_id = %s AND is_active = true
            """,
            (repository_uuid, principal.account_id),
        ).fetchone()
        if row is None:
            raise PermissionError("repository is not accessible")
        workspace = (self._workspace_root / str(repository_uuid)).resolve()
        try:
            workspace.relative_to(self._workspace_root)
        except ValueError as error:
            raise PermissionError("repository workspace escapes workspace root") from error
        return AuthorizedRepository(str(repository_uuid), principal.account_id, workspace)


class DashboardReader(Protocol):
    """Dashboard dependency used by the controller."""

    def upcoming_impact(self, account_id: str) -> list[ImpactSummary]:
        """Return the account's open impact summaries."""


class ScanController:
    """Translate client requests into queue jobs and dashboard responses."""

    def __init__(
        self,
        queue: JobQueue,
        dashboard: DashboardReader,
        repositories: RepositoryAuthorizer,
    ) -> None:
        self._queue = queue
        self._dashboard = dashboard
        self._repositories = repositories

    def submit_scan(
        self, principal: AuthenticatedPrincipal, request: ScanRequest
    ) -> ScanResponse:
        """Validate and enqueue a scan without performing pipeline work in the handler."""
        if not principal.account_id or not principal.account_id.strip():
            raise PermissionError("authenticated account_id is required")
        request = self._normalized_request(request)
        repository = self._repositories.resolve(principal, request.repository_id)
        if repository.account_id != principal.account_id:
            raise PermissionError("repository is not owned by the authenticated account")
        idempotency_key = self._idempotency_key(principal, request, repository.repository_id)
        job_id = self._queue.enqueue(
            "scan_repository",
            idempotency_key,
            {
                "account_id": principal.account_id,
                "repository_id": repository.repository_id,
                "provider": request.provider,
                "repository_path": str(repository.workspace),
                "commit_sha": request.commit_sha,
                "trigger": request.trigger,
            },
            priority=50,
        )
        return ScanResponse(
            job_id=job_id,
            status="accepted" if job_id is not None else "already_queued",
            idempotency_key=idempotency_key,
        )

    def dashboard(self, principal: AuthenticatedPrincipal) -> list[ImpactSummary]:
        """Return dashboard data through the service layer."""
        return self._dashboard.upcoming_impact(principal.account_id)

    @staticmethod
    def _normalized_request(request: ScanRequest) -> ScanRequest:
        """Validate and canonicalize request fields before authorization and queueing."""
        repo_id = request.repository_id.strip()
        if not repo_id:
            raise ValueError("repository_id must not be empty")
        if ".." in repo_id or repo_id.startswith("-") or not _REPO_ID_RE.fullmatch(repo_id):
            raise ValueError("invalid repository_id")
        commit_sha = request.commit_sha.strip()
        if not commit_sha:
            raise ValueError("commit_sha must not be empty")
        if commit_sha.startswith("-") or not _COMMIT_SHA_RE.fullmatch(commit_sha):
            raise ValueError("invalid commit_sha")
        provider = request.provider.strip()
        if not provider:
            raise ValueError("provider must not be empty")
        trigger = request.trigger.strip()
        if not trigger:
            raise ValueError("trigger must not be empty")
        if trigger not in _ALLOWED_TRIGGERS:
            raise ValueError(f"unsupported trigger {trigger!r}")
        get_provider(provider)
        return ScanRequest(repo_id, provider, commit_sha, trigger)

    @staticmethod
    def _idempotency_key(
        principal: AuthenticatedPrincipal,
        request: ScanRequest,
        canonical_repo_id: str | None = None,
    ) -> str:
        """Hash canonical, server-resolved identity and revision fields."""
        canonical = json.dumps(
            {
                "account_id": principal.account_id,
                "commit_sha": request.commit_sha,
                "provider": request.provider,
                "repository_id": canonical_repo_id or request.repository_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"scan:{sha256(canonical).hexdigest()}"
