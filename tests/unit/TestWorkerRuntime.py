from __future__ import annotations

from threading import Event

import pytest

from Driftwatch.Services.WorkerRuntime import WorkerRuntime, WorkerRuntimeConfig


class Worker:
    def __init__(
        self,
        *,
        stop_event: Event | None = None,
        stop_after: int | None = None,
        error: bool = False,
    ) -> None:
        self.calls = 0
        self.stop_event = stop_event
        self.stop_after = stop_after
        self.error = error

    def run_once(self) -> bool:
        self.calls += 1
        if self.error:
            self.error = False
            raise RuntimeError("temporary failure")
        if self.stop_after is not None and self.calls >= self.stop_after:
            if self.stop_event is not None:
                self.stop_event.set()
            return False
        return True


def test_runtime_processes_work_until_stop_requested() -> None:
    stop_event = Event()
    worker = Worker(stop_event=stop_event, stop_after=3)
    runtime = WorkerRuntime(
        worker,
        worker_name="indexer-1",
        config=WorkerRuntimeConfig(idle_delay_seconds=0),
    )

    runtime.run_forever(stop_event)

    assert worker.calls == 3


def test_runtime_continues_after_cycle_failure() -> None:
    stop_event = Event()
    worker = Worker(stop_event=stop_event, error=True, stop_after=2)
    runtime = WorkerRuntime(
        worker,
        worker_name="indexer-1",
        config=WorkerRuntimeConfig(idle_delay_seconds=0, error_delay_seconds=0),
    )

    runtime.run_forever(stop_event)

    assert worker.calls == 2


def test_runtime_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="idle_delay_seconds"):
        WorkerRuntimeConfig(idle_delay_seconds=-1)

    with pytest.raises(ValueError, match="worker_name"):
        WorkerRuntime(Worker(), worker_name=" ")
