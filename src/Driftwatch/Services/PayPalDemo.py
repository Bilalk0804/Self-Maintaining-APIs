"""Repeatable PayPal source-ingestion demo orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from Driftwatch.Services.JobQueue import JobQueue
from Driftwatch.Services.SourceIngestion import (
    SourceIndexWorker,
    SourcePollingCoordinator,
    SourceSnapshotRepository,
)
from Driftwatch.Watch.PayloadIndexers import OpenApiPayloadIndexer, PythonSdkPayloadIndexer
from Driftwatch.Watch.ProviderAdapters import provider_adapter


class DemoConnection(Protocol):
    """Minimal connection surface required by the PayPal demo."""

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        """Execute a parameterized query."""

    def commit(self) -> None:
        """Commit durable demo state."""


@dataclass(frozen=True, slots=True)
class PayPalSourceResult:
    """Outcome for one PayPal ground-truth source."""

    kind: str
    snapshot_id: UUID | None
    status: str
    indexed_entries: int


def run_paypal_demo(
    connection: DemoConnection,
    *,
    worker_id: str = "paypal-demo",
) -> tuple[PayPalSourceResult, ...]:
    """Poll and index all active PayPal ground-truth sources once."""
    rows = connection.execute(
        """
        SELECT s.id, s.kind, s.url
        FROM api_source s
        JOIN api_provider p ON p.id = s.provider_id
        WHERE p.slug = 'paypal' AND s.is_active = true AND s.is_ground_truth = true
        ORDER BY s.kind
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("no active PayPal ground-truth sources found")

    queue = JobQueue(connection)
    snapshots = SourceSnapshotRepository(connection)
    coordinator = SourcePollingCoordinator(connection, snapshots, queue)
    worker = SourceIndexWorker(
        queue,
        snapshots,
        {
            ("paypal", "openapi"): OpenApiPayloadIndexer("paypal"),
            ("paypal", "sdk_source"): PythonSdkPayloadIndexer("paypal"),
        },
        worker_id=worker_id,
    )
    pending: list[tuple[UUID, str, UUID | None]] = []
    for source_id, kind, source_url in rows:
        adapter = provider_adapter("paypal", kind, source_url=source_url)
        try:
            snapshot_id = coordinator.poll_once(
                source_id,
                adapter,
                correlation_id=worker_id,
            )
        finally:
            adapter.close()
        pending.append((source_id, kind, snapshot_id))

    while worker.run_once():
        pass

    results: list[PayPalSourceResult] = []
    for source_id, kind, snapshot_id in pending:
        if snapshot_id is None:
            row = connection.execute(
                """
                SELECT id, index_status, COALESCE(index_entry_count, 0)
                FROM source_snapshot
                WHERE source_id = %s AND index_status = 'built'
                ORDER BY fetched_at DESC
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if row is None:
                results.append(PayPalSourceResult(kind, None, "unchanged", 0))
            else:
                results.append(PayPalSourceResult(kind, row[0], "unchanged", row[2]))
            continue
        row = connection.execute(
            """
            SELECT index_status, COALESCE(index_entry_count, 0)
            FROM source_snapshot
            WHERE id = %s
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"PayPal snapshot disappeared: {snapshot_id}")
        results.append(PayPalSourceResult(kind, snapshot_id, row[0], row[1]))
    return tuple(results)
