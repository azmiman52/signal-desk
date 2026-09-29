"""Application foundation: process liveness and bounded database readiness."""

import asyncio
import os
from collections.abc import Awaitable, Callable

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

DatabaseProbe = Callable[[], Awaitable[bool]]


async def database_ready() -> bool:
    """Check a real database without exposing credentials or blocking the event loop.

    This is connectivity readiness only. Schema compatibility must be added with
    the first migration; collection freshness will be a separate signal.
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
            return await connection.fetchval("SELECT 1") == 1
    except (TimeoutError, asyncpg.PostgresError, OSError, ValueError):
        return False
    finally:
        if connection is not None:
            # A probe has no transaction to preserve; synchronous termination
            # keeps cleanup bounded even when the network disappears.
            connection.terminate()


def create_app(probe: DatabaseProbe = database_ready) -> FastAPI:
    app = FastAPI(title="SignalDesk API", version="0.1.0")

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
                "checks": {"database": "reachable" if healthy else "unavailable"},
                "scope": "database_connectivity",
            },
            status_code=200 if healthy else 503,
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
