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
            WITH new_job AS (
                INSERT INTO job (kind, idempotency_key)
                VALUES (%s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
            ), new_state AS (
                INSERT INTO job_state (
                    job_id, priority, max_attempts, run_after
                )
                SELECT id, %s, %s, COALESCE(%s, now()) FROM new_job
                RETURNING job_id
            )
            INSERT INTO job_payload (job_id, payload)
            SELECT job_id, %s::jsonb FROM new_state
            RETURNING job_id
        """
        row = self._connection.execute(
            query,
            (
                kind,
                idempotency_key,
                priority,
                max_attempts,
                run_after,
                orjson.dumps(payload or {}).decode(),
            ),
        ).fetchone()
        self._connection.commit()
        return None if row is None else row[0]

    def claim(self, worker_id: str) -> JobClaim | None:
        """Atomically lease the highest-priority ready job."""
        query = """
            UPDATE job_state
            SET status = 'running', locked_by = %s, locked_at = now(),
                attempts = attempts + 1
            WHERE job_id = (
                SELECT id
                FROM job
                JOIN job_state ON job_state.job_id = job.id
                WHERE job_state.status = 'pending' AND job_state.run_after <= now()
                ORDER BY job_state.priority ASC, job.created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING job_id, attempts
        """
        row = self._connection.execute(query, (worker_id,)).fetchone()
        self._connection.commit()
        if row is None:
            return None
        payload_row = self._connection.execute(
            """
                SELECT job.kind, job_payload.payload
                FROM job
                JOIN job_payload ON job_payload.job_id = job.id
                WHERE job.id = %s
            """,
            (row[0],),
        ).fetchone()
        self._connection.commit()
        if payload_row is None:
            raise RuntimeError(f"job payload missing for claimed job {row[0]}")
        return JobClaim(
            id=row[0],
            kind=payload_row[0],
            payload=payload_row[1],
            attempts=row[1],
        )

    def succeed(self, job_id: UUID) -> None:
        """Mark a leased job as successfully completed."""
        self._update_status(job_id, "succeeded")

    def fail(self, job_id: UUID, error: str, *, retry_after: datetime | None = None) -> None:
        """Retry a failed job until its configured attempt limit is exhausted."""
        query = """
            UPDATE job_state
            SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
                run_after = COALESCE(%s, now()),
                locked_by = NULL, locked_at = NULL
            WHERE job_id = %s AND status = 'running'
        """
        self._connection.execute(query, (retry_after, job_id))
        self._connection.execute(
            """
                INSERT INTO job_run (job_id, attempt_no, worker_id, exit_status, error)
                SELECT id, attempts, COALESCE(locked_by, 'unknown'), 'failed', %s
                FROM job_state WHERE job_id = %s
            """,
            (error, job_id),
        )
        self._connection.commit()

    def _update_status(self, job_id: UUID, status: str) -> None:
        self._connection.execute(
            """
                UPDATE job_state
                SET status = %s, locked_by = NULL, locked_at = NULL
                WHERE job_id = %s AND status = 'running'
            """,
            (status, job_id),
        )
        self._connection.commit()
