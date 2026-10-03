import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text

from signaldesk.config import Settings
from signaldesk.db import accounts, make_engine, sessions
from signaldesk.main import create_app
from signaldesk.security import digest


@pytest.fixture
async def database():
    url = os.environ.get("TEST_DATABASE_URL")
    assert url, "TEST_DATABASE_URL must identify a dedicated test database"
    # Refuse accidental execution on a normal application database.
    from urllib.parse import urlsplit

    assert urlsplit(url).path.rsplit("/", 1)[-1].endswith("_test")
    admin = make_engine(url)
    schema = "sdtest_" + uuid4().hex
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = make_engine(url, connect_args={"server_settings": {"search_path": schema}})
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    async with db.begin() as conn:

        def migrate(sync_conn):
            config.attributes["connection"] = sync_conn
            command.upgrade(config, "head")

        await conn.run_sync(migrate)
    try:
        yield db
    finally:
        await db.dispose()
        async with admin.begin() as conn:
            # schema is generated above, never read from input/config.
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
async def harness(database):
    settings = Settings(
        app_env="development",
        app_origin="http://127.0.0.1:8080",
        github_client_id="fixture-client",
        github_client_secret="fixture-secret",
        github_redirect_uri="http://127.0.0.1:8080/api/v1/auth/github/callback",
    )
    app = create_app(settings=settings, db_engine=database)
    clients = []

    async def client_for(owner=None, *, provider_id=None):
        token, csrf = uuid4().hex + uuid4().hex, uuid4().hex
        now = datetime.now(UTC)
        async with database.begin() as conn:
            if owner is None:
                owner = uuid4()
                await conn.execute(
                    insert(accounts).values(
                        id=owner,
                        github_user_id=provider_id or (int(uuid4().hex[:12], 16) + 1),
                        display_name="Test owner",
                    )
                )
            session_id = uuid4()
            await conn.execute(
                insert(sessions).values(
                    id=session_id,
                    account_id=owner,
                    token_hash=digest(token),
                    csrf_token=csrf,
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + timedelta(days=7),
                )
            )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=settings.app_origin,
            headers={"Origin": settings.app_origin, "X-CSRF-Token": csrf},
        )
        client.cookies.set(settings.cookie_name, token)
        clients.append(client)
        return SimpleNamespace(
            client=client, owner=owner, session_id=session_id, token=token, csrf=csrf
        )

    yield SimpleNamespace(app=app, db=database, settings=settings, client_for=client_for)
    for client in clients:
        await client.aclose()
