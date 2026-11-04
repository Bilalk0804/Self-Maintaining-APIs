"""Long-running runtime for durable background workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event
from typing import Protocol

logger = logging.getLogger(__name__)


class WorkerCycle(Protocol):
    """One unit of durable background work."""

    def run_once(self) -> bool:
        """Process one queued job and return whether work was available."""


@dataclass(frozen=True, slots=True)
class WorkerRuntimeConfig:
    """Bounded delays used when a worker is idle or encounters an error."""

    idle_delay_seconds: float = 1.0
    error_delay_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.idle_delay_seconds < 0:
            raise ValueError("idle_delay_seconds must not be negative")
        if self.error_delay_seconds < 0:
            raise ValueError("error_delay_seconds must not be negative")


class WorkerRuntime:
    """Keep a durable worker alive until a stop event is requested."""

    def __init__(
        self,
        worker: WorkerCycle,
        *,
        worker_name: str,
        config: WorkerRuntimeConfig | None = None,
    ) -> None:
        if not worker_name.strip():
            raise ValueError("worker_name must not be empty")
        self._worker = worker
        self._worker_name = worker_name
        self._config = config or WorkerRuntimeConfig()

    def run_forever(self, stop_event: Event) -> None:
        """Process jobs until ``stop_event`` is set, including during backoff."""
        logger.info("worker_started", extra={"worker_name": self._worker_name})
        try:
            while not stop_event.is_set():
                try:
                    had_work = self._worker.run_once()
                except Exception:
                    logger.exception(
                        "worker_cycle_failed",
                        extra={"worker_name": self._worker_name},
                    )
                    self._wait(stop_event, self._config.error_delay_seconds)
                    continue
                if not had_work:
                    self._wait(stop_event, self._config.idle_delay_seconds)
        finally:
            logger.info("worker_stopped", extra={"worker_name": self._worker_name})

    @staticmethod
    def _wait(stop_event: Event, delay_seconds: float) -> None:
        """Wait interruptibly so shutdown does not depend on the polling delay."""
        stop_event.wait(delay_seconds)
