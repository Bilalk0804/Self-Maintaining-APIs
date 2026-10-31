"""PostgreSQL-backed queue for durable pipeline work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import orjson


class SqlConnection(Protocol):
    """Minimal connection surface required by :class:`JobQueue`."""

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        """Execute a parameterized SQL statement."""

    def commit(self) -> None:
        """Commit the current transaction."""


@dataclass(frozen=True, slots=True)
class JobClaim:
    """A job leased to a worker."""

    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int


class JobQueue:
    """Enqueue and lease jobs using PostgreSQL row locking."""

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def enqueue(
        self,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
        *,
        priority: int = 100,
        max_attempts: int = 3,
        run_after: datetime | None = None,
    ) -> UUID | None:
        """Insert a job once, returning its identifier when newly created."""
        query = """
            INSERT INTO job (
                kind, idempotency_key, payload, priority, max_attempts, run_after
            )
            VALUES (%s, %s, %s::jsonb, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
        """
        row = self._connection.execute(
            query,
            (
                kind,
                idempotency_key,
                orjson.dumps(payload or {}).decode(),
                priority,
                max_attempts,
                run_after,
            ),
        ).fetchone()
        self._connection.commit()
        return None if row is None else row[0]

    def claim(self, worker_id: str) -> JobClaim | None:
        """Atomically lease the highest-priority ready job."""
        query = """
            UPDATE job
            SET status = 'running', locked_by = %s, locked_at = now(),
                attempts = attempts + 1
            WHERE id = (
                SELECT id
                FROM job
                WHERE status = 'pending' AND run_after <= now()
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, kind, payload, attempts
        """
        row = self._connection.execute(query, (worker_id,)).fetchone()
        self._connection.commit()
        if row is None:
            return None
        return JobClaim(
            id=row[0],
            kind=row[1],
            payload=row[2],
            attempts=row[3],
        )

    def succeed(self, job_id: UUID) -> None:
        """Mark a leased job as successfully completed."""
        self._update_status(job_id, "succeeded")

    def fail(self, job_id: UUID, error: str, *, retry_after: datetime | None = None) -> None:
        """Retry a failed job until its configured attempt limit is exhausted."""
        query = """
            UPDATE job
            SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
                run_after = COALESCE(%s, now()),
                locked_by = NULL, locked_at = NULL
            WHERE id = %s AND status = 'running'
        """
        self._connection.execute(query, (retry_after, job_id))
        self._connection.execute(
            """
                INSERT INTO job_run (job_id, attempt_no, worker_id, exit_status, error)
                SELECT id, attempts, COALESCE(locked_by, 'unknown'), 'failed', %s
                FROM job WHERE id = %s
            """,
            (error, job_id),
        )
        self._connection.commit()

    def _update_status(self, job_id: UUID, status: str) -> None:
        self._connection.execute(
            """
                UPDATE job
                SET status = %s, locked_by = NULL, locked_at = NULL
                WHERE id = %s AND status = 'running'
            """,
            (status, job_id),
        )
        self._connection.commit()
