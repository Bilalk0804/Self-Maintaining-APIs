"""Durable raw-payload ingestion and provider index processing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import orjson

from Driftwatch.Services.JobQueue import JobQueue
from Driftwatch.Watch.PayloadIndexers import IndexedPayload, PayloadIndexer, PayloadIndexingError


class SourceIngestionConnection(Protocol):
    """Minimal SQL connection used by durable source ingestion."""

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        """Execute a parameterized statement."""

    def commit(self) -> None:
        """Commit the current transaction."""


@dataclass(frozen=True, slots=True)
class IngestedSnapshot:
    """The durable identity returned after raw-payload deduplication."""

    id: UUID
    source_id: UUID
    content_sha256: str
    created: bool


class SourceSnapshotRepository:
    """Persist raw snapshots and atomically update their normalized index state."""

    def __init__(self, connection: SourceIngestionConnection) -> None:
        self._connection = connection

    def ingest(
        self,
        source_id: UUID,
        payload: bytes,
        *,
        content_type: str | None = None,
        upstream_ref: str | None = None,
        fetched_at: datetime | None = None,
    ) -> IngestedSnapshot:
        """Persist bytes before any parsing and deduplicate by source/hash."""
        digest = hashlib.sha256(payload).hexdigest()
        row = self._connection.execute(
            """
                INSERT INTO source_snapshot (
                    source_id, content_sha256, raw_payload, byte_size,
                    content_type, upstream_ref, fetched_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
                ON CONFLICT (source_id, content_sha256) DO NOTHING
                RETURNING id
            """,
            (
                source_id,
                digest,
                payload,
                len(payload),
                content_type,
                upstream_ref,
                fetched_at,
            ),
        ).fetchone()
        self._connection.commit()
        if row is not None:
            return IngestedSnapshot(row[0], source_id, digest, True)
        existing = self._connection.execute(
            """
                SELECT id FROM source_snapshot
                WHERE source_id = %s AND content_sha256 = %s
            """,
            (source_id, digest),
        ).fetchone()
        self._connection.commit()
        if existing is None:
            raise RuntimeError("snapshot disappeared after idempotent insert")
        return IngestedSnapshot(existing[0], source_id, digest, False)

    def load_for_index(self, snapshot_id: UUID) -> tuple[bytes, str, str]:
        """Load raw bytes and source metadata for one snapshot."""
        row = self._connection.execute(
            """
                SELECT ss.raw_payload, p.slug, s.kind
                FROM source_snapshot ss
                JOIN api_source s ON s.id = ss.source_id
                JOIN api_provider p ON p.id = s.provider_id
                WHERE ss.id = %s
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"source snapshot not found: {snapshot_id}")
        return row[0], row[1], row[2]

    def mark_indexed(self, snapshot_id: UUID, indexed: IndexedPayload) -> None:
        """Persist the complete normalized index and mark the snapshot built together."""
        entries = {
            ref: {
                "kind": descriptor.kind,
                "type_sig": descriptor.type_sig,
                "required": descriptor.required,
                "nullable": descriptor.nullable,
                "default": descriptor.default,
                "enum_values": descriptor.enum_values,
                "deprecated": descriptor.deprecated,
                "sunset_on": descriptor.sunset_on,
                "doc_hash": descriptor.doc_hash,
                "source_locator": descriptor.source_locator,
            }
            for ref, descriptor in indexed.index.entries.items()
        }
        self._connection.execute(
            """
                INSERT INTO source_snapshot_index (
                    snapshot_id, provider, revision, entries, entry_count
                )
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (snapshot_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    revision = EXCLUDED.revision,
                    entries = EXCLUDED.entries,
                    entry_count = EXCLUDED.entry_count,
                    built_at = now()
            """,
            (
                snapshot_id,
                indexed.provider,
                indexed.revision,
                orjson.dumps(entries).decode(),
                len(entries),
            ),
        )
        self._connection.execute(
            """
                UPDATE source_snapshot
                SET index_status = 'built', index_error = NULL,
                    indexed_at = now(), index_entry_count = %s
                WHERE id = %s
            """,
            (len(entries), snapshot_id),
        )
        self._connection.commit()

    def mark_index_failed(self, snapshot_id: UUID, error: str) -> None:
        """Quarantine malformed payloads without losing their raw bytes."""
        self._connection.execute(
            """
                UPDATE source_snapshot
                SET index_status = 'quarantined', index_error = %s
                WHERE id = %s
            """,
            (error[:2000], snapshot_id),
        )
        self._connection.commit()


class SourceIndexWorker:
    """Process durable indexing jobs without allowing parser errors to kill the worker."""

    def __init__(
        self,
        queue: JobQueue,
        snapshots: SourceSnapshotRepository,
        indexers: dict[tuple[str, str], PayloadIndexer],
        *,
        worker_id: str,
    ) -> None:
        self._queue = queue
        self._snapshots = snapshots
        self._indexers = indexers
        self._worker_id = worker_id

    def run_once(self) -> bool:
        """Claim and process one indexing job; return whether work was available."""
        claim = self._queue.claim(self._worker_id)
        if claim is None:
            return False
        try:
            if claim.kind != "index_source_snapshot":
                raise ValueError(f"unsupported source job kind: {claim.kind}")
            snapshot_id = UUID(str(claim.payload["snapshot_id"]))
            raw, provider, source_kind = self._snapshots.load_for_index(snapshot_id)
            indexer = self._indexers[(provider, source_kind)]
            indexed = indexer.index(raw, revision=str(claim.payload.get("revision", "unknown")))
            self._snapshots.mark_indexed(snapshot_id, indexed)
        except (KeyError, TypeError, ValueError, PayloadIndexingError) as error:
            if "snapshot_id" in claim.payload:
                self._snapshots.mark_index_failed(
                    UUID(str(claim.payload["snapshot_id"])),
                    str(error),
                )
            self._queue.fail(claim.id, str(error))
            return True
        except Exception as error:
            self._queue.fail(claim.id, "unexpected source indexing failure")
            raise RuntimeError("unexpected source indexing failure") from error
        self._queue.succeed(claim.id)
        return True
