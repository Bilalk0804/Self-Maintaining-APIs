from __future__ import annotations

from uuid import UUID

from Driftwatch.Services.JobQueue import JobQueue


class Cursor:
    def __init__(self, row: tuple | None) -> None:
        self.row = row

    def fetchone(self) -> tuple | None:
        return self.row


class Connection:
    def __init__(self, *rows: tuple | None) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []
        self.commits = 0

    def execute(self, query: str, params: tuple = ()) -> Cursor:
        self.queries.append(query)
        return Cursor(self.rows.pop(0) if self.rows else None)

    def commit(self) -> None:
        self.commits += 1


def test_enqueue_is_idempotent() -> None:
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    connection = Connection((job_id,))

    result = JobQueue(connection).enqueue("poll_source", "source:stripe:latest")

    assert result == job_id
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in connection.queries[0]
    assert connection.commits == 1


def test_claim_uses_skip_locked() -> None:
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    connection = Connection((job_id, "poll_source", {"provider": "stripe"}, 1))

    claim = JobQueue(connection).claim("worker-1")

    assert claim is not None
    assert claim.id == job_id
    assert claim.payload == {"provider": "stripe"}
    assert "FOR UPDATE SKIP LOCKED" in connection.queries[0]
