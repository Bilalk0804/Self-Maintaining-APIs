from __future__ import annotations

from uuid import uuid4

import pytest

from Driftwatch.Services.ScheduledPolling import ScheduledPollingWorker


class Result:
    def __init__(self, row: tuple | None = None) -> None:
        self.row = row

    def fetchone(self) -> tuple | None:
        return self.row


class Connection:
    def __init__(self, rows: list[tuple | None]) -> None:
        self.rows = iter(rows)
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple = ()) -> Result:
        self.queries.append((query, params))
        return Result(next(self.rows))

    def commit(self) -> None:
        pass


class Coordinator:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def poll_once(self, source_id: object, adapter: object, **kwargs: object) -> None:
        self.calls.append((source_id, adapter, kwargs))


class Adapter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_scheduler_claims_polls_and_releases_source() -> None:
    source_id = uuid4()
    connection = Connection(
        [
            (source_id, "razorpay", "sdk_source", "https://example.test/sdk.py", 900),
            None,
            (source_id,),
            None,
        ]
    )
    coordinator = Coordinator()
    adapter = Adapter()
    created: list[tuple[str, str, str]] = []

    def factory(provider: str, kind: str, url: str) -> Adapter:
        created.append((provider, kind, url))
        return adapter

    worker = ScheduledPollingWorker(
        connection,
        coordinator,  # type: ignore[arg-type]
        factory,
        worker_id="poller-1",
    )

    assert worker.run_once() is True
    assert created == [("razorpay", "sdk_source", "https://example.test/sdk.py")]
    assert coordinator.calls[0][0] == source_id
    assert coordinator.calls[0][2]["correlation_id"] == "poller-1"
    assert adapter.closed is True


def test_scheduler_returns_false_when_no_source_is_due() -> None:
    connection = Connection([None])

    worker = ScheduledPollingWorker(connection, Coordinator(), lambda *_: Adapter(), worker_id="p")

    assert worker.run_once() is False


def test_scheduler_rejects_invalid_configuration() -> None:
    connection = Connection([None])

    with pytest.raises(ValueError, match="worker_id"):
        ScheduledPollingWorker(connection, Coordinator(), lambda *_: Adapter(), worker_id=" ")
    with pytest.raises(ValueError, match="lease_seconds"):
        ScheduledPollingWorker(
            connection,
            Coordinator(),
            lambda *_: Adapter(),
            worker_id="p",
            lease_seconds=0,
        )
