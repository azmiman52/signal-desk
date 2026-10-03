import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from signaldesk.db import accounts, oauth_attempts, projects, sessions
from signaldesk.security import digest

pytestmark = pytest.mark.integration


def idem():
    return {"Idempotency-Key": str(uuid4())}


async def create(client, name="API"):
    return await client.post("/api/v1/projects", json={"name": name}, headers=idem())


async def test_owner_isolation_pagination_and_validation(harness):
    a, b = await harness.client_for(), await harness.client_for()
    created = await create(a.client)
    assert created.status_code == 201
    project = created.json()
    path = "/api/v1/projects/" + project["id"]
    assert (await b.client.get(path)).status_code == 404
    for suffix in ("archive", "restore"):
        assert (
            await b.client.post(path + "/" + suffix, json={"expected_version": 1}, headers=idem())
        ).status_code == 404
    assert (
        await b.client.patch(path, json={"name": "stolen", "expected_version": 1})
    ).status_code == 404
    assert (await b.client.get("/api/v1/projects")).json()["total"] == 0
    listing = (await a.client.get("/api/v1/projects?page=2&page_size=1")).json()
    assert listing["items"] == [] and listing["total"] == 1
    assert listing["account_id"] == str(a.owner)
    assert (
        await a.client.post("/api/v1/projects", json={"name": "  "}, headers=idem())
    ).status_code == 422
    assert (
        await a.client.post(
            "/api/v1/projects", json={"name": "x", "account_id": str(b.owner)}, headers=idem()
        )
    ).status_code == 422


async def test_concurrent_capacity_and_archival_restore(harness):
    a = await harness.client_for()
    responses = await asyncio.gather(*(create(a.client, f"project {i}") for i in range(8)))
    assert sorted(r.status_code for r in responses) == [201] * 3 + [409] * 5
    project = next(r.json() for r in responses if r.status_code == 201)
    path = "/api/v1/projects/" + project["id"]
    archived = await a.client.post(path + "/archive", json={"expected_version": 1}, headers=idem())
    assert archived.status_code == 200 and archived.json()["version"] == 2
    assert (await a.client.get("/api/v1/projects")).json()["total"] == 2
    assert (await a.client.get("/api/v1/projects?include_archived=true")).json()["total"] == 3
    raced = await asyncio.gather(
        create(a.client, "new"),
        a.client.post(path + "/restore", json={"expected_version": 2}, headers=idem()),
    )
    assert sorted(r.status_code for r in raced) in ([200, 409], [201, 409])
    async with harness.db.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count()).select_from(projects).where(projects.c.archived_at.is_(None))
            )
            == 3
        )


async def test_idempotency_conflicts_and_lifecycle(harness):
    a = await harness.client_for()
    headers = idem()
    results = await asyncio.gather(
        *(
            a.client.post("/api/v1/projects", json={"name": "same"}, headers=headers)
            for _ in range(5)
        )
    )
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()["id"] for r in results}) == 1
    assert sum(r.headers.get("idempotency-replayed") == "true" for r in results) == 4
    assert (
        await a.client.post("/api/v1/projects", json={"name": "different"}, headers=headers)
    ).status_code == 409
    path = "/api/v1/projects/" + results[0].json()["id"]
    edits = await asyncio.gather(
        *(
            a.client.patch(path, json={"name": name, "expected_version": 1}, headers=idem())
            for name in ("A", "B")
        )
    )
    assert sorted(r.status_code for r in edits) == [200, 409]
    assert next(r for r in edits if r.status_code == 409).json()["error"]["latest"]["version"] == 2
    archived = await a.client.post(path + "/archive", json={"expected_version": 2}, headers=idem())
    assert archived.status_code == 200
    assert (
        await a.client.post("/api/v1/projects", json={"name": "same"}, headers=headers)
    ).status_code == 409


@pytest.mark.parametrize("fault", ["origin", "csrf", "unicode"])
async def test_csrf_rejects_before_writes(harness, fault):
    a = await harness.client_for()
    headers = idem()
    headers.update(
        {"Origin": "https://attacker.example"}
        if fault == "origin"
        else {"X-CSRF-Token": b"\xe9" if fault == "unicode" else "wrong"}
    )
    assert (
        await a.client.post("/api/v1/projects", json={"name": "blocked"}, headers=headers)
    ).status_code == 403
    assert (await a.client.get("/api/v1/projects")).json()["total"] == 0


@pytest.mark.parametrize("fault", ["absolute", "idle", "revoked"])
async def test_expiry_and_revoke_reject_cached_replay(harness, fault):
    a = await harness.client_for()
    headers = idem()
    assert (
        await a.client.post("/api/v1/projects", json={"name": "one"}, headers=headers)
    ).status_code == 201
    now = datetime.now(UTC)
    values = (
        {"expires_at": now - timedelta(seconds=1)}
        if fault == "absolute"
        else (
            {"last_seen_at": now - timedelta(hours=25)} if fault == "idle" else {"revoked_at": now}
        )
    )
    async with harness.db.begin() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == a.session_id).values(**values))
    response = await a.client.post("/api/v1/projects", json={"name": "one"}, headers=headers)
    assert response.status_code == 401 and "name" not in response.json()


async def test_logout_all_and_timezone(harness):
    a = await harness.client_for()
    second = await harness.client_for(a.owner)
    b = await harness.client_for()
    assert (
        await a.client.patch("/api/v1/account", json={"timezone": "Asia/Bangkok"})
    ).status_code == 200
    assert (
        await a.client.patch("/api/v1/account", json={"timezone": "Not/AZone"})
    ).status_code == 422
    assert (await a.client.post("/api/v1/auth/logout-all")).status_code == 204
    assert (await second.client.get("/api/v1/session")).status_code == 401
    assert (await b.client.get("/api/v1/session")).status_code == 200


async def start_oauth(harness, client):
    response = await client.get("/api/v1/auth/github/start")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    return query["state"][0]


async def test_oauth_browser_binding_one_use_and_hashed_session(harness):
    provider = AsyncMock()
    provider.identity.return_value = (812345, "Fixture GitHub user")
    harness.app.state.oauth_provider = provider
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url=harness.settings.app_origin
    ) as browser:
        state = await start_oauth(harness, browser)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app), base_url=harness.settings.app_origin
        ) as attacker:
            rejected = await attacker.get(
                "/api/v1/auth/github/callback", params={"state": state, "code": "fixture"}
            )
            assert "invalid_oauth_state" in rejected.headers["location"]
        results = await asyncio.gather(
            *(
                browser.get(
                    "/api/v1/auth/github/callback", params={"state": state, "code": "fixture"}
                )
                for _ in range(2)
            )
        )
        assert sum(r.headers["location"] == "/projects" for r in results) == 1
        provider.identity.assert_awaited_once()
        success = next(r for r in results if r.headers["location"] == "/projects")
        token = success.cookies[harness.settings.cookie_name]
        async with harness.db.connect() as conn:
            stored = (await conn.execute(select(sessions))).mappings().one()
            assert stored["token_hash"] == digest(token) and token != stored["token_hash"]
            assert (stored["expires_at"] - stored["created_at"]).days == 7
            assert await conn.scalar(select(func.count()).select_from(oauth_attempts)) == 0
        assert (
            "HttpOnly" in success.headers["set-cookie"]
            and "SameSite=lax" in success.headers["set-cookie"]
        )


async def test_oauth_expired_state_and_denial(harness):
    provider = AsyncMock()
    harness.app.state.oauth_provider = provider
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url=harness.settings.app_origin
    ) as browser:
        state = await start_oauth(harness, browser)
        async with harness.db.begin() as conn:
            await conn.execute(
                update(oauth_attempts).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        response = await browser.get(
            "/api/v1/auth/github/callback", params={"state": state, "code": "fixture"}
        )
        assert "invalid_oauth_state" in response.headers["location"]
        state = await start_oauth(harness, browser)
        response = await browser.get(
            "/api/v1/auth/github/callback", params={"state": state, "error": "access_denied"}
        )
        assert "oauth_denied" in response.headers["location"]
        provider.identity.assert_not_called()


async def test_oauth_rate_limit(harness):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url=harness.settings.app_origin
    ) as browser:
        results = [await browser.get("/api/v1/auth/github/start") for _ in range(21)]
        assert results[-1].status_code == 429
        assert results[-1].headers["retry-after"] == "60"


async def test_migration_head_and_foreign_keys(harness):
    from sqlalchemy import text

    async with harness.db.connect() as conn:
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_identity_projects"
        )
        assert await conn.scalar(select(func.count()).select_from(accounts)) == 0


async def test_admission_cap_and_existing_account_login(harness):
    from dataclasses import replace

    harness.app.state.settings = replace(harness.settings, max_accounts=1)
    provider = AsyncMock()
    harness.app.state.oauth_provider = provider
    a = await harness.client_for(provider_id=555)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url=harness.settings.app_origin
    ) as browser:
        provider.identity.return_value = (999, "new account")
        state = await start_oauth(harness, browser)
        response = await browser.get(
            "/api/v1/auth/github/callback", params={"state": state, "code": "fixture"}
        )
        assert "capacity_exceeded" in response.headers["location"]
        provider.identity.return_value = (555, "existing account")
        state = await start_oauth(harness, browser)
        response = await browser.get(
            "/api/v1/auth/github/callback", params={"state": state, "code": "fixture"}
        )
        assert response.headers["location"] == "/projects"
        assert (await browser.get("/api/v1/session")).json()["account"]["id"] == str(a.owner)


async def test_login_rotation_and_csrf_binding(harness):
    a, b = await harness.client_for(provider_id=551), await harness.client_for(provider_id=552)
    provider = AsyncMock()
    provider.identity.return_value = (552, "B")
    harness.app.state.oauth_provider = provider
    state = await start_oauth(harness, a.client)
    response = await a.client.get(
        "/api/v1/auth/github/callback", params={"state": state, "code": "fixture"}
    )
    assert response.headers["location"] == "/projects"
    async with harness.db.connect() as conn:
        assert await conn.scalar(select(sessions.c.revoked_at).where(sessions.c.id == a.session_id))
    # Old session A's CSRF cannot act through new session B's browser cookie.
    response = await create(a.client)
    assert response.status_code == 403
    assert (await b.client.get("/api/v1/session")).status_code == 200


async def test_logout_races_write_and_blocks_later_replay(harness):
    a = await harness.client_for()
    writer = await harness.client_for(a.owner)
    key = idem()
    results = await asyncio.gather(
        a.client.post("/api/v1/auth/logout-all"),
        writer.client.post("/api/v1/projects", json={"name": "race"}, headers=key),
    )
    assert results[0].status_code == 204
    assert results[1].status_code in (201, 401)
    assert (
        await writer.client.post("/api/v1/projects", json={"name": "race"}, headers=key)
    ).status_code == 401
