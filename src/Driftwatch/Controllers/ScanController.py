"""Framework-neutral scan and dashboard controller draft."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from Driftwatch.Providers import get_provider
from Driftwatch.Services.Growth import ImpactSummary
from Driftwatch.Services.JobQueue import JobQueue


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """Validated request received from an HTTP or CLI client."""

    account_id: str
    repository_id: str
    provider: str
    repository_path: str
    commit_sha: str
    trigger: str = "manual"


@dataclass(frozen=True, slots=True)
class ScanResponse:
    """Accepted scan response returned to a client."""

    job_id: UUID | None
    status: str
    idempotency_key: str


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
    ) -> None:
        self._queue = queue
        self._dashboard = dashboard

    def submit_scan(self, request: ScanRequest) -> ScanResponse:
        """Validate and enqueue a scan without performing pipeline work in the handler."""
        self._validate_request(request)
        idempotency_key = self._idempotency_key(request)
        job_id = self._queue.enqueue(
            "scan_repository",
            idempotency_key,
            {
                "account_id": request.account_id,
                "repository_id": request.repository_id,
                "provider": request.provider,
                "repository_path": request.repository_path,
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

    def dashboard(self, account_id: str) -> list[ImpactSummary]:
        """Return dashboard data through the service layer."""
        if not account_id.strip():
            raise ValueError("account_id must not be empty")
        return self._dashboard.upcoming_impact(account_id)

    @staticmethod
    def _validate_request(request: ScanRequest) -> None:
        """Reject incomplete requests before they reach the queue."""
        if not request.account_id.strip():
            raise ValueError("account_id must not be empty")
        if not request.repository_id.strip():
            raise ValueError("repository_id must not be empty")
        if not request.repository_path.strip():
            raise ValueError("repository_path must not be empty")
        if not request.commit_sha.strip():
            raise ValueError("commit_sha must not be empty")
        if not request.trigger.strip():
            raise ValueError("trigger must not be empty")
        get_provider(request.provider)

    @staticmethod
    def _idempotency_key(request: ScanRequest) -> str:
        """Build a stable key for one repository revision and provider."""
        return ":".join(
            (
                "scan",
                request.account_id,
                request.repository_id,
                request.provider,
                request.commit_sha,
            )
        )
