from __future__ import annotations

from uuid import UUID, uuid4

from Driftwatch.Services.JobQueue import JobClaim
from Driftwatch.Services.SourceIngestion import SourceIndexWorker
from Driftwatch.Watch.PayloadIndexers import RazorpaySdkPayloadIndexer


class Queue:
    def __init__(self, claim: JobClaim) -> None:
        self.claim_value = claim
        self.succeeded: list[UUID] = []
        self.failed: list[tuple[UUID, str]] = []

    def claim(self, worker_id: str) -> JobClaim | None:
        return self.claim_value

    def succeed(self, job_id: UUID) -> None:
        self.succeeded.append(job_id)

    def fail(self, job_id: UUID, error: str, **kwargs: object) -> None:
        self.failed.append((job_id, error))


class Snapshots:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.failed: list[tuple[UUID, str]] = []
        self.indexed = []

    def load_for_index(self, snapshot_id: UUID) -> tuple[bytes, str, str]:
        return self.raw, "razorpay", "sdk_source"

    def mark_indexed(self, snapshot_id: UUID, indexed: object) -> None:
        self.indexed.append((snapshot_id, indexed))

    def mark_index_failed(self, snapshot_id: UUID, error: str) -> None:
        self.failed.append((snapshot_id, error))


def claim(payload: dict[str, object]) -> JobClaim:
    return JobClaim(uuid4(), "index_source_snapshot", payload, 1)


def test_worker_indexes_snapshot_and_completes_job() -> None:
    job_id = uuid4()
    queue = Queue(JobClaim(job_id, "index_source_snapshot", {"snapshot_id": str(uuid4())}, 1))
    snapshots = Snapshots(
        b"def create(self):\n return self.request('/v1/payments', method='POST')"
    )
    worker = SourceIndexWorker(
        queue,
        snapshots,
        {("razorpay", "sdk_source"): RazorpaySdkPayloadIndexer()},
        worker_id="worker-1",
    )

    assert worker.run_once() is True
    assert queue.succeeded == [job_id]
    assert len(snapshots.indexed) == 1
    assert snapshots.failed == []


def test_worker_quarantines_bad_payload_and_retries_job() -> None:
    job_id = uuid4()
    queue = Queue(JobClaim(job_id, "index_source_snapshot", {"snapshot_id": str(uuid4())}, 1))
    snapshots = Snapshots(b"def broken(:")
    worker = SourceIndexWorker(
        queue,
        snapshots,
        {("razorpay", "sdk_source"): RazorpaySdkPayloadIndexer()},
        worker_id="worker-1",
    )

    assert worker.run_once() is True
    assert queue.succeeded == []
    assert queue.failed[0][0] == job_id
    assert snapshots.failed
