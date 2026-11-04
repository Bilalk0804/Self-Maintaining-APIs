"""Lease-based scheduling for durable provider source polling."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from Driftwatch.Services.SourceIngestion import PollingAdapter, SourcePollingCoordinator

logger = logging.getLogger(__name__)


class PollingConnection(Protocol):
    """Database surface required by the source scheduler."""

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        """Execute a parameterized query."""

    def commit(self) -> None:
        """Commit the current transaction."""


class PollingAdapterFactory(Protocol):
    """Create an adapter for one persisted provider source."""

    def __call__(self, provider: str, source_kind: str, url: str) -> PollingAdapter:
        """Return a configured polling adapter."""


@dataclass(frozen=True, slots=True)
class DueSource:
    """Provider source metadata claimed by one scheduler instance."""

    id: UUID
    provider: str
    kind: str
    url: str
    poll_interval_seconds: int


class ScheduledPollingWorker:
    """Claim and poll one due provider source per worker cycle."""

    def __init__(
        self,
        connection: PollingConnection,
        coordinator: SourcePollingCoordinator,
        adapter_factory: PollingAdapterFactory,
        *,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._connection = connection
        self._coordinator = coordinator
        self._adapter_factory = adapter_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def run_once(self) -> bool:
        """Claim and poll one source, returning whether a source was due."""
        source = self._claim_due_source()
        if source is None:
            return False
        adapter: PollingAdapter | None = None
        try:
            adapter = self._adapter_factory(source.provider, source.kind, source.url)
            self._coordinator.poll_once(source.id, adapter, correlation_id=self._worker_id)
        except Exception:
            logger.exception(
                "source_poll_failed",
                extra={"worker_id": self._worker_id, "source_id": str(source.id)},
            )
        finally:
            self._release_lease(source)
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        return True

    def _claim_due_source(self) -> DueSource | None:
        row = self._connection.execute(
            """
                SELECT s.id, p.slug, s.kind, s.url, s.poll_interval_s
                FROM api_source s
                JOIN api_provider p ON p.id = s.provider_id
                LEFT JOIN api_source_state state ON state.source_id = s.id
                WHERE s.is_active = true
                  AND p.is_active = true
                  AND (state.next_poll_at IS NULL OR state.next_poll_at <= now())
                  AND (
                      state.lease_owner IS NULL
                      OR state.lease_expires_at < now()
                      OR state.lease_owner = %s
                  )
                ORDER BY COALESCE(state.next_poll_at, '-infinity'::timestamptz), s.id
                LIMIT 1
            """,
            (self._worker_id,),
        ).fetchone()
        if row is None:
            self._connection.commit()
            return None
        source = DueSource(row[0], row[1], row[2], row[3], row[4])
        self._connection.execute(
            """
                INSERT INTO api_source_state (
                    source_id, lease_owner, lease_expires_at, updated_at
                )
                VALUES (%s, %s, now() + (%s * interval '1 second'), now())
                ON CONFLICT (source_id) DO NOTHING
            """,
            (source.id, self._worker_id, self._lease_seconds),
        )
        claimed = self._connection.execute(
            """
                UPDATE api_source_state
                SET lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE source_id = %s
                  AND (lease_owner IS NULL OR lease_expires_at < now() OR lease_owner = %s)
                  AND (next_poll_at IS NULL OR next_poll_at <= now())
                RETURNING source_id
            """,
            (self._worker_id, self._lease_seconds, source.id, self._worker_id),
        ).fetchone()
        self._connection.commit()
        return source if claimed is not None else None

    def _release_lease(self, source: DueSource) -> None:
        self._connection.execute(
            """
                UPDATE api_source_state
                SET lease_owner = NULL,
                    lease_expires_at = NULL,
                    next_poll_at = now() + (GREATEST(%s, 1) * interval '1 second'),
                    updated_at = now()
                WHERE source_id = %s AND lease_owner = %s
            """,
            (source.poll_interval_seconds, source.id, self._worker_id),
        )
        self._connection.commit()
