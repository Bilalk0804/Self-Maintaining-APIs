"""Uvicorn entry point for the configured Driftwatch HTTP application."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg

from Driftwatch.Controllers.Auth import Auth0TokenValidator
from Driftwatch.Controllers.ScanController import (
    PostgresRepositoryAuthorizer,
    ScanController,
)
from Driftwatch.Http.Application import create_app
from Driftwatch.Services.Growth import DashboardService
from Driftwatch.Services.JobQueue import JobQueue


def build_runtime_app() -> Any:
    """Construct the application from required runtime configuration."""
    database_url = _required_env("DRIFTWATCH_DATABASE_URL")
    issuer = _required_env("DRIFTWATCH_AUTH0_ISSUER")
    audience = _required_env("DRIFTWATCH_AUTH0_AUDIENCE")
    workspace_root = Path(_required_env("DRIFTWATCH_WORKSPACE_ROOT")).resolve()
    connection = psycopg.connect(database_url)
    queue = JobQueue(connection)
    dashboard = DashboardService(connection)
    repositories = PostgresRepositoryAuthorizer(connection, workspace_root)

    def readiness_check() -> bool:
        try:
            connection.execute("SELECT 1").fetchone()
        except psycopg.Error:
            return False
        return True

    return create_app(
        ScanController(queue, dashboard, repositories),
        Auth0TokenValidator(issuer, audience),
        readiness_check=readiness_check,
        shutdown=connection.close,
        enable_api_docs=_env_bool("DRIFTWATCH_ENABLE_API_DOCS", default=False),
    )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"{name} must be configured before starting the API")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise RuntimeError(f"{name} must be true or false")
    return normalized == "true"


app = build_runtime_app()
