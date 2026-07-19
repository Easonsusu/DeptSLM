"""FastAPI application for DeptSLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from app import __version__
from app.audit import LoggingAuditSink
from app.auth import AuthenticatedPrincipal, build_token_verifier
from app.authorization import require_authenticated_principal
from app.database import create_database_engine, create_session_factory
from app.document_storage import DocumentStorage
from app.membership_resolver import SQLAlchemyMembershipResolver
from app.routes import router as department_router
from app.settings import Settings


class HealthResponse(BaseModel):
    """Response returned by the health check."""

    status: str


class VersionResponse(BaseModel):
    """Response returned by the version endpoint."""

    name: str
    version: str


class IdentityResponse(BaseModel):
    """Safe authenticated identity metadata."""

    subject: str
    issuer: str


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate required configuration before serving requests."""

    settings = Settings.from_environment()
    application.state.settings = settings
    application.state.token_verifier = build_token_verifier(settings)
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.membership_resolver = SQLAlchemyMembershipResolver(session_factory)
    application.state.audit_sink = LoggingAuditSink()
    application.state.document_storage = DocumentStorage(settings.data_dir)
    # Tests may inject reviewed fakes. Production constructs short-lived clients
    # inside the answer service so no external call occurs during application startup.
    application.state.rag_runtime_client = None
    application.state.rag_qdrant = None
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(
    title="DeptSLM API",
    description="Backend API for the DeptSLM departmental assistant platform.",
    version=__version__,
    lifespan=lifespan,
)
app.include_router(department_router)


@app.get("/health", response_model=HealthResponse, tags=["system"])
def get_health() -> HealthResponse:
    """Report whether the API process is running."""

    return HealthResponse(status="ok")


@app.get("/version", response_model=VersionResponse, tags=["system"])
def get_version() -> VersionResponse:
    """Report the public project and API version."""

    return VersionResponse(name="DeptSLM", version=__version__)


@app.get("/auth/me", response_model=IdentityResponse, tags=["authentication"])
def get_current_identity(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
) -> IdentityResponse:
    """Return safe metadata for the authenticated principal."""

    return IdentityResponse(subject=principal.subject, issuer=principal.issuer)
