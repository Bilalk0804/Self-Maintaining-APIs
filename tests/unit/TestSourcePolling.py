from __future__ import annotations

from uuid import UUID, uuid4

from Driftwatch.Services.SourceIngestion import (
    IngestedSnapshot,
    SourcePollingCoordinator,
)
from Driftwatch.Watch.SourceAdapters import PollResult, SourceState


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.executed: list[tuple[str, tuple]] = []

    def fetchone(self) -> tuple | None:
        return self.rows.pop(0) if self.rows else None


class Connection:
    def __init__(self) -> None:
        self.results: list[Result] = []
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple = ()) -> Result:
        self.queries.append((query, params))
        return self.results.pop(0) if self.results else Result()

    def commit(self) -> None:
        pass


class Adapter:
    def __init__(self, result: PollResult) -> None:
        self.result = result
        self.states: list[SourceState] = []

    def poll(self, state: SourceState) -> PollResult:
        self.states.append(state)
        return self.result


class Snapshots:
    def __init__(self, snapshot: IngestedSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[bytes] = []

    def ingest(self, source_id: UUID, payload: bytes, **kwargs: object) -> IngestedSnapshot:
        self.calls.append(payload)
        return self.snapshot


class Queue:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def enqueue(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


def test_changed_poll_is_persisted_and_enqueued() -> None:
    source_id = uuid4()
    snapshot_id = uuid4()
    connection = Connection()
    connection.results.append(Result())
    snapshots = Snapshots(IngestedSnapshot(snapshot_id, source_id, "hash", True))
    queue = Queue()
    adapter = Adapter(PollResult(True, b"payload", SourceState(etag="etag"), 200))

    result = SourcePollingCoordinator(connection, snapshots, queue).poll_once(
        source_id, adapter, correlation_id="corr-1"
    )

    assert result == snapshot_id
    assert adapter.states == [SourceState()]
    assert snapshots.calls == [b"payload"]
    assert queue.calls[0][0][0] == "index_source_snapshot"
    assert queue.calls[0][0][1] == f"index:{source_id}:hash"


def test_unchanged_poll_does_not_ingest_or_enqueue() -> None:
    source_id = uuid4()
    connection = Connection()
    state_result = Result()
    state_result.rows.append(("old", "yesterday", "hash"))
    connection.results.append(state_result)
    snapshots = Snapshots(IngestedSnapshot(uuid4(), source_id, "hash", False))
    queue = Queue()
    adapter = Adapter(PollResult(False, None, SourceState("old", "yesterday", "hash"), 304))

    result = SourcePollingCoordinator(connection, snapshots, queue).poll_once(
        source_id, adapter
    )
    assert result is None
    assert snapshots.calls == []
    assert queue.calls == []
    assert adapter.states == [SourceState("old", "yesterday", "hash")]
