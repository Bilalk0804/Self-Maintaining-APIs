"""Run the existing source poller and indexer without a person in the loop."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Protocol

from Driftwatch.Services.JobQueue import JobQueue
from Driftwatch.Services.ScheduledPolling import ScheduledPollingWorker
from Driftwatch.Services.SourceIngestion import SourcePollingCoordinator, SourceSnapshotRepository
from Driftwatch.Services.WorkerRuntime import WorkerCycle
from Driftwatch.Watch.ProviderAdapters import provider_adapter
from Driftwatch.Worker import build_worker

logger = logging.getLogger(__name__)

_STATUS_SQL = """
    SELECT p.slug, s.kind, s.url, s.poll_interval_s,
           state.last_polled_at, state.next_poll_at, state.consecutive_failures,
           latest.status, latest.error_message,
           snap.index_status, snap.index_entry_count
    FROM api_source s
    JOIN api_provider p ON p.id = s.provider_id
    LEFT JOIN api_source_state state ON state.source_id = s.id
    LEFT JOIN LATERAL (
        SELECT status, error_message
        FROM api_source_poll
        WHERE source_id = s.id
        ORDER BY started_at DESC
        LIMIT 1
    ) latest ON true
    LEFT JOIN LATERAL (
        SELECT index_status, index_entry_count
        FROM source_snapshot
        WHERE source_id = s.id
        ORDER BY fetched_at DESC
        LIMIT 1
    ) snap ON true
    WHERE s.is_active = true
      AND p.is_active = true
      AND s.is_ground_truth = true
    ORDER BY p.slug, s.kind
"""


class WatchConnection(Protocol):
    """SQL connection shared by the watcher and its status read."""

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        """Execute a parameterized query."""

    def commit(self) -> None:
        """Commit the current transaction."""


@dataclass(frozen=True, slots=True)
class SourceWatch:
    """One ground-truth source and the latest poll the backend recorded."""

    provider: str
    kind: str
    url: str
    poll_interval_seconds: int
    last_polled_at: str | None
    next_poll_at: str | None
    consecutive_failures: int
    last_status: str | None
    last_error: str | None
    index_status: str | None
    indexed_entries: int | None


@dataclass(frozen=True, slots=True)
class WatchBoard:
    """Sources the autonomous loop is responsible for."""

    sources: tuple[SourceWatch, ...]


class WatchStatus:
    """Read poll and index state already stored by the backend."""

    def __init__(self, connection: WatchConnection) -> None:
        self._connection = connection

    def load(self) -> WatchBoard:
        """Return the active ground-truth sources and their latest outcome."""
        rows = self._connection.execute(_STATUS_SQL).fetchall()
        self._connection.commit()
        return WatchBoard(tuple(_source_from_row(row) for row in rows))


class AutonomousWatch:
    """Poll one due source, then index one queued snapshot, per cycle."""

    def __init__(
        self,
        poller: WorkerCycle,
        indexer: WorkerCycle,
        status: WatchStatus,
    ) -> None:
        self._poller = poller
        self._indexer = indexer
        self._status = status
        self._lock = Lock()

    def run_once(self) -> bool:
        """Search once and index once. Return whether either found work."""
        with self._lock:
            polled = self._poller.run_once()
            indexed = self._indexer.run_once()
        if polled or indexed:
            logger.info(
                "watch_cycle",
                extra={"polled": polled, "indexed": indexed},
            )
        return polled or indexed

    def status(self) -> WatchBoard:
        """Read the current source board without starting a poll."""
        with self._lock:
            return self._status.load()


def build_autonomous_watch(connection: Any, *, worker_id: str) -> AutonomousWatch:
    """Wire the watcher to the same poller and indexer the processes use.

    Args:
        connection: Open Postgres connection. The caller owns its lifetime.
        worker_id: Identity recorded on poll leases and index jobs.

    Returns:
        A watch cycle safe for one background thread plus status reads.
    """
    if not worker_id.strip():
        raise ValueError("worker_id must not be empty")
    queue = JobQueue(connection)
    snapshots = SourceSnapshotRepository(connection)
    poller = ScheduledPollingWorker(
        connection,
        SourcePollingCoordinator(connection, snapshots, queue),
        _provider_adapter,
        worker_id=worker_id,
    )
    return AutonomousWatch(
        poller,
        build_worker(connection, worker_id=worker_id),
        WatchStatus(connection),
    )


def _provider_adapter(provider: str, kind: str, url: str) -> object:
    return provider_adapter(provider, kind, source_url=url)


def _source_from_row(row: tuple[Any, ...]) -> SourceWatch:
    entries = row[10]
    return SourceWatch(
        provider=str(row[0]),
        kind=str(row[1]),
        url=str(row[2]),
        poll_interval_seconds=int(row[3]),
        last_polled_at=_iso(row[4]),
        next_poll_at=_iso(row[5]),
        consecutive_failures=int(row[6] or 0),
        last_status=row[7] if isinstance(row[7], str) else None,
        last_error=row[8] if isinstance(row[8], str) else None,
        index_status=row[9] if isinstance(row[9], str) else None,
        indexed_entries=int(entries) if entries is not None else None,
    )


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None
