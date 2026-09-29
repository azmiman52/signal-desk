import asyncio
from unittest.mock import AsyncMock, Mock

import asyncpg
import httpx
import pytest

from signaldesk.main import create_app, database_ready


async def test_liveness_does_not_probe_database():
    probe = AsyncMock(return_value=False)
    transport = httpx.ASGITransport(app=create_app(probe))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    probe.assert_not_awaited()


@pytest.mark.parametrize("healthy,code", [(True, 200), (False, 503)])
async def test_readiness_is_honest_and_uncached(healthy, code):
    transport = httpx.ASGITransport(app=create_app(AsyncMock(return_value=healthy)))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == code
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["scope"] == "database_connectivity"
    assert response.json()["checks"]["database"] == ("reachable" if healthy else "unavailable")


async def test_missing_configuration_is_not_ready(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    connect = AsyncMock()
    monkeypatch.setattr(asyncpg, "connect", connect)
    assert not await database_ready()
    connect.assert_not_awaited()


@pytest.mark.parametrize("error", [OSError("secret-host"), TimeoutError(), ValueError("bad URL")])
async def test_connection_errors_are_not_exposed(monkeypatch, error):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret:secret@invalid/test")
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=error))
    assert not await database_ready()


async def test_query_failure_terminates_connection(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    connection = Mock(fetchval=AsyncMock(side_effect=asyncpg.PostgresError("private data")))
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))
    assert not await database_ready()
    connection.terminate.assert_called_once()


async def test_overall_deadline_bounds_a_stalled_connect(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")

    async def stalled(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(asyncpg, "connect", stalled)
    started = asyncio.get_running_loop().time()
    assert not await database_ready()
    assert asyncio.get_running_loop().time() - started < 4
