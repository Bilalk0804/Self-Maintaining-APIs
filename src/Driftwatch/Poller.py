"""Executable process for scheduled provider source polling."""

from __future__ import annotations

import os
import signal
import socket
from threading import Event

import psycopg

from Driftwatch.Services.JobQueue import JobQueue
from Driftwatch.Services.ScheduledPolling import ScheduledPollingWorker
from Driftwatch.Services.SourceIngestion import SourcePollingCoordinator, SourceSnapshotRepository
from Driftwatch.Services.WorkerRuntime import WorkerRuntime, WorkerRuntimeConfig
from Driftwatch.Watch.SourceAdapters import HttpSourceAdapter


def main() -> None:
    """Run scheduled source polling until the process receives a termination signal."""
    connection = psycopg.connect(_required_env("DRIFTWATCH_DATABASE_URL"))
    worker_id = _env("DRIFTWATCH_POLLER_ID", f"poller-{socket.gethostname()}-{os.getpid()}")
    stop_event = Event()
    _install_signal_handlers(stop_event)
    snapshots = SourceSnapshotRepository(connection)
    coordinator = SourcePollingCoordinator(connection, snapshots, JobQueue(connection))
    poller = ScheduledPollingWorker(
        connection,
        coordinator,
        lambda _provider, _kind, url: HttpSourceAdapter(url),
        worker_id=worker_id,
        lease_seconds=_env_int("DRIFTWATCH_POLLER_LEASE_SECONDS", 300),
    )
    runtime = WorkerRuntime(
        poller,
        worker_name=worker_id,
        config=WorkerRuntimeConfig(
            idle_delay_seconds=_env_float("DRIFTWATCH_POLLER_IDLE_DELAY", 1.0),
            error_delay_seconds=_env_float("DRIFTWATCH_POLLER_ERROR_DELAY", 5.0),
        ),
    )
    try:
        runtime.run_forever(stop_event)
    finally:
        connection.close()


def _install_signal_handlers(stop_event: Event) -> None:
    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"{name} must be configured before starting the poller")
    return value


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive")
    return parsed


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number") from error
    if parsed < 0:
        raise RuntimeError(f"{name} must not be negative")
    return parsed


if __name__ == "__main__":
    main()
