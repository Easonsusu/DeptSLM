"""FastAPI application for DeptSLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__
from app.settings import Settings


class HealthResponse(BaseModel):
    """Response returned by the health check."""

    status: str


class VersionResponse(BaseModel):
    """Response returned by the version endpoint."""

    name: str
    version: str


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate required configuration before serving requests."""

    application.state.settings = Settings.from_environment()
    yield


app = FastAPI(
    title="DeptSLM API",
    description="Backend API for the DeptSLM departmental assistant platform.",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse, tags=["system"])
def get_health() -> HealthResponse:
    """Report whether the API process is running."""

    return HealthResponse(status="ok")


@app.get("/version", response_model=VersionResponse, tags=["system"])
def get_version() -> VersionResponse:
    """Report the public project and API version."""

    return VersionResponse(name="DeptSLM", version=__version__)
