"""Thin FastAPI application around authenticated Driftwatch controllers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from Driftwatch.Controllers.Auth import (
    Auth0TokenValidator,
    AuthenticatedPrincipal,
    AuthenticationError,
)
from Driftwatch.Controllers.ScanController import ScanController, ScanRequest
from Driftwatch.Services.PayPalCheckout import CapturedPayment, CheckoutOrder, PayPalCheckoutError


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


class CheckoutResponse(BaseModel):
    """Sandbox order for the fixed Driftwatch workspace plan."""

    order_id: str
    approve_url: str
    plan: str
    amount: str
    currency: str


class CaptureResponse(BaseModel):
    """Sandbox capture of an approved workspace order."""

    order_id: str
    capture_id: str
    status: str
    amount: str
    currency: str


class CheckoutService(Protocol):
    """Create and capture the sandbox workspace plan."""

    def create_order(self) -> CheckoutOrder:
        """Create a sandbox order."""

    def capture(self, order_id: str) -> CapturedPayment:
        """Capture an approved sandbox order."""


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
    shutdown: Callable[[], None] | None = None,
    enable_api_docs: bool = True,
    checkout: CheckoutService | None = None,
) -> FastAPI:
    """Build a dependency-injected application suitable for fake GCP/gcloud tests."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if shutdown is not None:
                shutdown()

    app = FastAPI(
        title="Driftwatch API",
        version="v1",
        lifespan=lifespan,
        docs_url="/docs" if enable_api_docs else None,
        redoc_url="/redoc" if enable_api_docs else None,
        openapi_url="/openapi.json" if enable_api_docs else None,
    )

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
    async def permission_error_handler(request: Request, error: PermissionError) -> JSONResponse:
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

    if enable_api_docs:

        @app.get("/api-explorer", include_in_schema=False)
        async def api_explorer() -> RedirectResponse:
            """Open the interactive Swagger UI for local API exploration."""
            return RedirectResponse("/docs", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

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

    @app.post("/v1/billing/checkout", response_model=CheckoutResponse)
    async def create_checkout() -> CheckoutResponse:
        """Create a PayPal sandbox order for the monthly workspace plan."""
        if checkout is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PayPal sandbox is not configured",
            )
        try:
            order = checkout.create_order()
        except PayPalCheckoutError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(error),
            ) from error
        return CheckoutResponse(
            order_id=order.order_id,
            approve_url=order.approve_url,
            plan=order.plan,
            amount=order.amount,
            currency=order.currency,
        )

    @app.post("/v1/billing/orders/{order_id}/capture", response_model=CaptureResponse)
    async def capture_checkout(order_id: str) -> CaptureResponse:
        """Capture an approved PayPal sandbox order."""
        if checkout is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PayPal sandbox is not configured",
            )
        try:
            payment = checkout.capture(order_id)
        except PayPalCheckoutError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(error),
            ) from error
        return CaptureResponse(
            order_id=payment.order_id,
            capture_id=payment.capture_id,
            status=payment.status,
            amount=payment.amount,
            currency=payment.currency,
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
