from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest

from Driftwatch.Services.JobQueue import JobQueue
from Driftwatch.Services.SourceIngestion import (
    SourceIndexWorker,
    SourcePollingCoordinator,
    SourceSnapshotRepository,
)
from Driftwatch.Watch.PayloadIndexers import RazorpaySdkPayloadIndexer
from Driftwatch.Watch.SourceAdapters import PollResult, SourceState


class Adapter:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def poll(self, state: SourceState) -> PollResult:
        return PollResult(
            changed=True,
            payload=self.payload,
            state=SourceState(content_hash="fixture-hash"),
            status_code=200,
        )


@pytest.mark.integration
def test_postgres_poll_snapshot_enqueue_and_index() -> None:
    connection_url = os.getenv(
        "DRIFTWATCH_TEST_DATABASE_URL",
        "host=127.0.0.1 dbname=driftwatch_dev user=postgres",
    )
    source_id: UUID
    payload = (
        f"def create(self):\n return self.request('/v1/payments', method='POST')\n"
        f"# fixture-{uuid4()}"
    ).encode()
    with psycopg.connect(connection_url) as connection:
        provider_id = connection.execute(
            "SELECT id FROM api_provider WHERE slug = 'razorpay'"
        ).fetchone()
        assert provider_id is not None
        source_id_row = connection.execute(
            """
                SELECT id FROM api_source
                WHERE provider_id = %s AND kind = 'sdk_source'
            """,
            (provider_id[0],),
        ).fetchone()
        assert source_id_row is not None
        source_id = source_id_row[0]

        queue = JobQueue(connection)
        snapshots = SourceSnapshotRepository(connection)
        snapshot_id = SourcePollingCoordinator(connection, snapshots, queue).poll_once(
            source_id, Adapter(payload), correlation_id="integration-source"
        )
        assert snapshot_id is not None

        worker = SourceIndexWorker(
            queue,
            snapshots,
            {("razorpay", "sdk_source"): RazorpaySdkPayloadIndexer()},
            worker_id="integration-indexer",
        )
        assert worker.run_once() is True
        status = connection.execute(
            "SELECT index_status FROM source_snapshot WHERE id = %s",
            (snapshot_id,),
        ).fetchone()
        assert status == ("built",)
        index = connection.execute(
            """
                SELECT entry_count, entries
                FROM source_snapshot_index
                WHERE snapshot_id = %s
            """,
            (snapshot_id,),
        ).fetchone()
        assert index is not None
        assert index[0] == 1
        assert "api:razorpay/op:post_v1_payments" in index[1]

        connection.execute(
            """
                DELETE FROM job
                WHERE id IN (
                    SELECT job_id FROM job_payload
                    WHERE payload->>'snapshot_id' = %s
                )
            """,
            (str(snapshot_id),),
        )
        connection.execute("DELETE FROM source_snapshot WHERE id = %s", (snapshot_id,))
        connection.execute("DELETE FROM api_source_state WHERE source_id = %s", (source_id,))
        connection.execute("DELETE FROM api_source_poll WHERE source_id = %s", (source_id,))
        connection.commit()
