"""Thin FastAPI application around authenticated Driftwatch controllers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from Driftwatch.Controllers.Auth import (
    Auth0TokenValidator,
    AuthenticatedPrincipal,
    AuthenticationError,
)
from Driftwatch.Controllers.ScanController import ScanController, ScanRequest


class ScanBody(BaseModel):
    """Public scan request; account and filesystem fields are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    commit_sha: str = Field(min_length=1, max_length=64)
    trigger: str = Field(default="manual", min_length=1, max_length=20)


class ScanAccepted(BaseModel):
    """Response returned after a scan is accepted by the durable queue."""

    job_id: str | None
    status: str
    idempotency_key: str


class ImpactResponse(BaseModel):
    """Stable JSON representation of an impact summary."""

    model_config = ConfigDict(from_attributes=True)

    change_id: str
    provider: str
    change_type: str
    severity: str
    title: str
    deadline_on: str | None
    days_remaining: int | None
    repos_affected: int
    findings_open: int
    best_confidence: float | None


def create_app(
    controller: ScanController,
    token_validator: Auth0TokenValidator,
    *,
    readiness_check: Callable[[], bool] | None = None,
) -> FastAPI:
    """Build a dependency-injected application suitable for fake GCP/gcloud tests."""
    app = FastAPI(title="Driftwatch API", version="v1")

    def authenticated_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedPrincipal:
        token = _bearer_token(authorization)
        try:
            return token_validator.validate(token)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    @app.exception_handler(PermissionError)
    async def permission_error_handler(
        request: Request, error: PermissionError
    ) -> JSONResponse:
        return _json_error(status.HTTP_403_FORBIDDEN, "forbidden")

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, error: ValueError) -> JSONResponse:
        return _json_error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        ready = readiness_check is None or readiness_check()
        if not ready:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not ready")
        return {"status": "ready"}

    @app.post("/v1/scans", response_model=ScanAccepted, status_code=status.HTTP_202_ACCEPTED)
    async def submit_scan(
        body: ScanBody,
        principal: AuthenticatedPrincipal = Depends(authenticated_principal),  # noqa: B008
    ) -> ScanAccepted:
        response = controller.submit_scan(
            principal,
            ScanRequest(
                repository_id=body.repository_id,
                provider=body.provider,
                commit_sha=body.commit_sha,
                trigger=body.trigger,
            ),
        )
        return ScanAccepted(
            job_id=None if response.job_id is None else str(response.job_id),
            status=response.status,
            idempotency_key=response.idempotency_key,
        )

    @app.get("/v1/dashboard/impact", response_model=list[ImpactResponse])
    async def dashboard(
        principal: AuthenticatedPrincipal = Depends(authenticated_principal),  # noqa: B008
    ) -> list[ImpactResponse]:
        return [ImpactResponse.model_validate(item) for item in controller.dashboard(principal)]

    return app


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def _json_error(code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": detail})
