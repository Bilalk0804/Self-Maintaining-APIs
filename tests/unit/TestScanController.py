from __future__ import annotations

from uuid import UUID

import pytest

from Driftwatch.Controllers.ScanController import ScanController, ScanRequest


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


def request() -> ScanRequest:
    return ScanRequest("account-1", "repo-1", "stripe", "/repos/app", "abc123")


def test_controller_enqueues_scan_and_returns_accepted() -> None:
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    queue = Queue(job_id)

    response = ScanController(queue, Dashboard()).submit_scan(request())

    assert response.job_id == job_id
    assert response.status == "accepted"
    assert response.idempotency_key == "scan:account-1:repo-1:stripe:abc123"
    assert queue.calls[0][0][0] == "scan_repository"


def test_controller_reports_existing_idempotent_job() -> None:
    response = ScanController(Queue(None), Dashboard()).submit_scan(request())

    assert response.status == "already_queued"


def test_controller_rejects_unknown_provider() -> None:
    invalid = ScanRequest("account", "repo", "unknown", "/repo", "sha")

    with pytest.raises(ValueError, match="unsupported provider"):
        ScanController(Queue(None), Dashboard()).submit_scan(invalid)


def test_controller_delegates_dashboard_reads() -> None:
    assert ScanController(Queue(None), Dashboard()).dashboard("account-1") == ["account-1"]
