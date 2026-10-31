from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from Driftwatch.Services.JobQueue import JobQueue


@pytest.mark.integration
def test_postgres_queue_enqueue_claim_and_succeed() -> None:
    connection_url = os.getenv(
        "DRIFTWATCH_TEST_DATABASE_URL",
        "host=127.0.0.1 dbname=driftwatch_dev user=postgres",
    )
    key = f"integration:{uuid4()}"
    with psycopg.connect(connection_url) as connection:
        queue = JobQueue(connection)
        job_id = queue.enqueue("integration_check", key, {"source": "pytest"}, priority=1)
        assert job_id is not None

        claim = queue.claim("integration-worker")
        assert claim is not None
        assert claim.id == job_id
        assert claim.payload == {"source": "pytest"}

        queue.succeed(job_id)
        status = connection.execute(
            """
                SELECT status FROM job_state
                WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
        assert status == ("succeeded",)

        connection.execute("DELETE FROM job WHERE id = %s", (job_id,))
        connection.commit()
