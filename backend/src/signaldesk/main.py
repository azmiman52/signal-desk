"""Application foundation: process liveness and bounded database readiness."""

import asyncio
import math
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from signaldesk.api import router
from signaldesk.config import Settings
from signaldesk.db import SCHEMA_HEAD, make_engine
from signaldesk.errors import APIError, api_error
from signaldesk.github_releases import CollectionError, GitHubReleases
from signaldesk.provider import GitHubProvider
from signaldesk.subscriptions_api import router as subscriptions_router

DatabaseProbe = Callable[[], Awaitable[bool]]


async def database_ready() -> bool:
    """Check a real database without exposing credentials or blocking the event loop.

    Checks connectivity and the expected migration head; collection freshness
    remains a separate signal.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return False
    connection = None
    try:
        async with asyncio.timeout(3):
            connection = await asyncpg.connect(
                database_url,
                timeout=2,
                command_timeout=1,
                server_settings={"statement_timeout": "1000"},
            )
            if await connection.fetchval("SELECT 1") != 1:
                return False
            return (
                await connection.fetchval("SELECT version_num FROM alembic_version") == SCHEMA_HEAD
            )
    except (TimeoutError, asyncpg.PostgresError, OSError, ValueError):
        return False
    finally:
        if connection is not None:
            # A probe has no transaction to preserve; synchronous termination
            # keeps cleanup bounded even when the network disappears.
            connection.terminate()


def create_app(
    probe: DatabaseProbe = database_ready,
    *,
    settings=None,
    db_engine=None,
    oauth_provider=None,
    release_provider=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    owned_engine = db_engine is None
    db_engine = db_engine or (make_engine(settings.database_url) if settings.database_url else None)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned_engine and app.state.engine is not None:
            await app.state.engine.dispose()

    app = FastAPI(title="SignalDesk API", version="0.2.0", lifespan=lifespan, debug=False)
    app.state.settings = settings
    app.state.engine = db_engine
    app.state.oauth_provider = oauth_provider or GitHubProvider()
    app.state.release_provider = release_provider or GitHubReleases(
        db_engine, token=settings.github_collector_token or None
    )
    app.include_router(router)
    app.include_router(subscriptions_router)
    app.add_exception_handler(APIError, api_error)

    @app.exception_handler(CollectionError)
    async def collection_error(request, exc):
        headers = {}
        if exc.retry_at:
            seconds = max(1, math.ceil((exc.retry_at - datetime.now(UTC)).total_seconds()))
            headers["Retry-After"] = str(seconds)
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "fields": {},
                    "retry_at": exc.retry_at.isoformat() if exc.retry_at else None,
                },
                "request_id": request.state.request_id,
            },
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo arbitrary submitted inputs (codes, tokens or private text).
        return await api_error(
            request, APIError(422, "validation_error", "Check the submitted fields.")
        )

    @app.middleware("http")
    async def safe_response(request, call_next):
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except (SQLAlchemyError, TimeoutError, OSError):
            # SQL exception strings can contain bind parameters. Do not log them.
            response = await api_error(
                request,
                APIError(503, "service_unavailable", "The service is temporarily unavailable."),
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health/live", tags=["health"])
    async def live() -> JSONResponse:
        return JSONResponse(
            {"status": "alive", "service": "signaldesk-api"},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health/ready", tags=["health"])
    async def ready() -> JSONResponse:
        healthy = await probe()
        return JSONResponse(
            {
                "status": "ready" if healthy else "not_ready",
                "checks": {
                    "database": "reachable" if healthy else "unavailable",
                    "schema": "compatible" if healthy else "unavailable",
                },
                "scope": "database_schema",
            },
            status_code=200 if healthy else 503,
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
