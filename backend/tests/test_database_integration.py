import os

import pytest

from signaldesk.main import database_ready


@pytest.mark.integration
async def test_real_postgres_readiness(monkeypatch):
    database_url = os.environ.get("TEST_DATABASE_URL")
    assert database_url, "Set TEST_DATABASE_URL to a dedicated test PostgreSQL database"
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert await database_ready(), "PostgreSQL readiness must execute SELECT 1 successfully"
