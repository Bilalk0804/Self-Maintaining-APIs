"""Executable process for durable source indexing jobs."""

from __future__ import annotations

import os
import signal
import socket
from threading import Event

import psycopg

from Driftwatch.Services.JobQueue import JobQueue
from Driftwatch.Services.SourceIngestion import SourceIndexWorker, SourceSnapshotRepository
from Driftwatch.Services.WorkerRuntime import WorkerRuntime, WorkerRuntimeConfig
from Driftwatch.Watch.PayloadIndexers import (
    OpenApiPayloadIndexer,
    PythonSdkPayloadIndexer,
    RazorpaySdkPayloadIndexer,
)


def build_worker(
    connection: psycopg.Connection[object],
    *,
    worker_id: str | None = None,
) -> SourceIndexWorker:
    """Build the source index worker with all currently supported indexers."""
    resolved_id = worker_id or _env("DRIFTWATCH_WORKER_ID", _default_worker_id())
    return SourceIndexWorker(
        JobQueue(connection),
        SourceSnapshotRepository(connection),
        {
            ("openai", "openapi"): OpenApiPayloadIndexer(),
            ("paypal", "openapi"): OpenApiPayloadIndexer("paypal"),
            ("paypal", "sdk_source"): PythonSdkPayloadIndexer("paypal"),
            ("stripe", "openapi"): OpenApiPayloadIndexer("stripe"),
            ("razorpay", "sdk_source"): RazorpaySdkPayloadIndexer(),
        },
        worker_id=resolved_id,
    )


def main() -> None:
    """Run the indexing worker until the process receives a termination signal."""
    connection = psycopg.connect(_required_env("DRIFTWATCH_DATABASE_URL"))
    stop_event = Event()
    _install_signal_handlers(stop_event)
    runtime = WorkerRuntime(
        build_worker(connection),
        worker_name=_env("DRIFTWATCH_WORKER_ID", _default_worker_id()),
        config=WorkerRuntimeConfig(
            idle_delay_seconds=_env_float("DRIFTWATCH_WORKER_IDLE_DELAY", 1.0),
            error_delay_seconds=_env_float("DRIFTWATCH_WORKER_ERROR_DELAY", 5.0),
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
        raise RuntimeError(f"{name} must be configured before starting the worker")
    return value


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


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


def _default_worker_id() -> str:
    return f"indexer-{socket.gethostname()}-{os.getpid()}"


if __name__ == "__main__":
    main()
