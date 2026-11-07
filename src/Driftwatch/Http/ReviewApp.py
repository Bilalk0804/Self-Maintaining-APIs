"""Loopback application that serves the finding-review desk and source watcher."""

from __future__ import annotations

import socket
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from typing import Literal, Protocol

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from Driftwatch.Services.AutonomousWatch import AutonomousWatch, build_autonomous_watch
from Driftwatch.Services.ReviewDesk import ReviewDesk
from Driftwatch.Services.WorkerRuntime import WorkerRuntime, WorkerRuntimeConfig

_REVIEW_DIR = Path(__file__).resolve().parent / "review"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class WatchStatusSource(Protocol):
    """Read-only view of the autonomous source search."""

    def status(self) -> object:
        """Return the current watch board."""


class VerdictBody(BaseModel):
    """Label submitted from the review page."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=400)
    verdict: Literal["true", "false", "unsure"] | None
    note: str = Field(default="", max_length=500)


def create_review_app(
    desk: ReviewDesk,
    watch: WatchStatusSource | None = None,
) -> FastAPI:
    """Build the local review application around one directory.

    Args:
        desk: Loaded review directory.
        watch: Backend source watcher. When present, the page reads its status.

    Returns:
        An application with the page, its assets, and the board API.
    """
    app = FastAPI(title="Driftwatch review", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_REVIEW_DIR / "index.html")

    @app.get("/api/watch")
    def watch_status() -> dict[str, object]:
        if watch is None:
            return {"connected": False, "sources": []}
        return {"connected": True, "sources": asdict(watch.status())["sources"]}

    @app.get("/api/board")
    def board() -> dict[str, object]:
        return asdict(desk.board())

    @app.put("/api/verdicts")
    def put_verdict(body: VerdictBody) -> dict[str, object]:
        try:
            desk.set_verdict(body.key, body.verdict, body.note)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="unknown finding",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        return asdict(desk.board())

    app.mount("/assets", StaticFiles(directory=_REVIEW_DIR), name="review-assets")
    return app


def serve_review(
    root: Path,
    *,
    host: str,
    port: int,
    database_url: str | None = None,
) -> None:
    """Serve the review desk and, when configured, search sources on their schedule.

    Args:
        root: Directory of finding JSON files. Created when missing.
        host: Bind address. Only loopback values are accepted.
        port: TCP port.
        database_url: Postgres URL for the poller and indexer. Without it, the page
            only reviews local files.

    Raises:
        ValueError: The host is not loopback or the database rejects the connection.
    """
    if host not in _LOOPBACK:
        raise ValueError("review desk binds to loopback only")
    root.mkdir(parents=True, exist_ok=True)
    connection = None
    watch: AutonomousWatch | None = None
    if database_url:
        connection, watch = _connect_watch(database_url)
    stop = Event()
    thread: Thread | None = None
    if watch is not None:
        runtime = WorkerRuntime(
            watch,
            worker_name=f"review-watch-{socket.gethostname()}",
            config=WorkerRuntimeConfig(idle_delay_seconds=1.0, error_delay_seconds=5.0),
        )
        thread = Thread(
            target=runtime.run_forever,
            args=(stop,),
            name="driftwatch-watch",
            daemon=True,
        )
        thread.start()
    try:
        uvicorn.run(
            create_review_app(ReviewDesk(root), watch),
            host=host,
            port=port,
            log_level="info",
        )
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)
        if connection is not None:
            connection.close()


def _connect_watch(database_url: str) -> tuple[object, AutonomousWatch]:
    import psycopg

    try:
        connection = psycopg.connect(database_url)
    except psycopg.Error as exc:
        raise ValueError("could not connect to the Driftwatch database") from exc
    worker_id = f"review-watch-{socket.gethostname()}"
    return connection, build_autonomous_watch(connection, worker_id=worker_id)
