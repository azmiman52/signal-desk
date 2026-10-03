import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update

from signaldesk.db import collection_runs, release_revisions, releases, sources, subscriptions
from signaldesk.github_releases import CollectionError

pytestmark = pytest.mark.integration


def key():
    return {"Idempotency-Key": str(uuid4())}


async def project(client, name="Application"):
    response = await client.post("/api/v1/projects", json={"name": name}, headers=key())
    assert response.status_code == 201, response.text
    return response.json()


def install_provider(harness):
    async def validate(value):
        repository = value.removeprefix("https://github.com/")
        suffix = repository.rsplit("/", 1)[-1]
        numeric = int(suffix.removeprefix("repo")) if suffix.startswith("repo") else 42
        return {
            "github_id": numeric + 100,
            "repository": repository,
            "url": "https://github.com/" + repository,
        }

    provider = AsyncMock()
    provider.validate_repository.side_effect = validate
    harness.app.state.release_provider = provider
    return provider


async def add(client, project_id, repository="owner/repo1", headers=None):
    return await client.post(
        f"/api/v1/projects/{project_id}/subscriptions",
        json={"repository": repository, "include_prereleases": False},
        headers=headers or key(),
    )


async def seeded_sources(harness, project_id, start, count):
    async with harness.db.begin() as conn:
        for i in range(start, start + count):
            source_id, subscription_id = uuid4(), uuid4()
            await conn.execute(
                insert(sources).values(
                    id=source_id,
                    github_id=10000 + i,
                    repository=f"seed/repo{i}",
                    url=f"https://github.com/seed/repo{i}",
                )
            )
            await conn.execute(
                insert(subscriptions).values(
                    id=subscription_id, project_id=UUID(project_id), source_id=source_id
                )
            )


async def test_subscription_owner_scope_and_safe_shapes(harness):
    provider = install_provider(harness)
    a, b = await harness.client_for(), await harness.client_for()
    p = await project(a.client)
    forbidden = await add(b.client, p["id"])
    assert forbidden.status_code == 404
    provider.validate_repository.assert_not_awaited()
    response = await add(a.client, p["id"])
    assert response.status_code == 201, response.text
    body = response.json()
    sub = body["subscription"]
    assert sub["account_id"] == str(a.owner) and sub["connection_state"] == "pending"
    assert sub["next_check_at"] is None
    sid = sub["id"]
    paths = [
        f"/api/v1/subscriptions/{sid}",
        f"/api/v1/subscriptions/{sid}/runs",
        f"/api/v1/subscriptions/{sid}/releases",
        f"/api/v1/subscriptions/{sid}/runs/{body['run']['id']}",
    ]
    for path in paths:
        assert (await b.client.get(path)).status_code == 404
    for action in ("pause", "resume", "remove"):
        assert (
            await b.client.post(
                f"/api/v1/subscriptions/{sid}/{action}", json={"expected_version": 1}, headers=key()
            )
        ).status_code == 404
    assert (
        await b.client.post(f"/api/v1/subscriptions/{sid}/checks", json={}, headers=key())
    ).status_code == 404
    listing = (await a.client.get(f"/api/v1/projects/{p['id']}/subscriptions")).json()
    assert listing["account_id"] == str(a.owner) and listing["total"] == 1
    run = (await a.client.get(paths[-1])).json()["run"]
    assert "lease_token" not in run and "source_id" not in run
    assert (await a.client.get(f"/api/v1/projects/{p['id']}")).json()["active_source_count"] == 1


async def test_add_idempotency_duplicate_and_readd_retains_identity(harness):
    provider = install_provider(harness)
    a = await harness.client_for()
    p = await project(a.client)
    headers = key()
    responses = await asyncio.gather(*(add(a.client, p["id"], headers=headers) for _ in range(3)))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    ids = {r.json()["subscription"]["id"] for r in responses}
    assert len(ids) == 1
    before = provider.validate_repository.await_count
    replay = await add(a.client, p["id"], headers=headers)
    assert replay.headers["idempotency-replayed"] == "true"
    assert provider.validate_repository.await_count == before
    assert (await add(a.client, p["id"])).status_code == 409
    sid = ids.pop()
    removed = await a.client.post(
        f"/api/v1/subscriptions/{sid}/remove", json={"expected_version": 1}, headers=key()
    )
    assert removed.status_code == 200
    assert (await add(a.client, p["id"], headers=headers)).status_code == 409
    restored = await add(a.client, p["id"])
    assert restored.status_code == 200, restored.text
    assert restored.json()["restored"] is True
    assert restored.json()["subscription"]["id"] == sid
    assert restored.json()["subscription"]["version"] == 3


async def test_reauthentication_after_provider_wait(harness):
    a = await harness.client_for()
    p = await project(a.client)
    began, finish = asyncio.Event(), asyncio.Event()

    async def validate(value):
        began.set()
        await finish.wait()
        return {
            "github_id": 123,
            "repository": "owner/repo1",
            "url": "https://github.com/owner/repo1",
        }

    provider = AsyncMock()
    provider.validate_repository.side_effect = validate
    harness.app.state.release_provider = provider
    adding = asyncio.create_task(add(a.client, p["id"]))
    await asyncio.wait_for(began.wait(), 3)
    # Logout must complete while provider is waiting: no account lock spans network.
    assert (await asyncio.wait_for(a.client.post("/api/v1/auth/logout-all"), 3)).status_code == 204
    finish.set()
    assert (await adding).status_code == 401
    async with harness.db.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(subscriptions)) == 0


async def test_source_capacity_race_distinct_shared_and_restore(harness):
    install_provider(harness)
    a = await harness.client_for()
    p1, p2 = await project(a.client, "one"), await project(a.client, "two")
    await seeded_sources(harness, p1["id"], 0, 19)
    results = await asyncio.gather(
        add(a.client, p1["id"], "owner/repo1"), add(a.client, p2["id"], "owner/repo2")
    )
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    winner = next(r.json()["subscription"] for r in results if r.status_code == 201)
    other = p2 if winner["project_id"] == p1["id"] else p1
    # A shared canonical source in another project consumes no new distinct slot.
    assert (await add(a.client, other["id"], winner["source"]["repository"])).status_code == 201
    archived = await a.client.post(
        f"/api/v1/projects/{p1['id']}/archive", json={"expected_version": 1}, headers=key()
    )
    assert archived.status_code == 200
    assert (await add(a.client, p2["id"], "owner/repo99")).status_code == 201
    restore = await a.client.post(
        f"/api/v1/projects/{p1['id']}/restore", json={"expected_version": 2}, headers=key()
    )
    assert restore.status_code == 409 and restore.json()["error"]["code"] == "capacity_exceeded"


async def test_pause_resume_conflict_and_manual_coalescing(harness):
    install_provider(harness)
    a = await harness.client_for()
    p = await project(a.client)
    body = (await add(a.client, p["id"])).json()
    sid = body["subscription"]["id"]
    base = f"/api/v1/subscriptions/{sid}"
    checks = await asyncio.gather(
        *(a.client.post(base + "/checks", json={}, headers=key()) for _ in range(5))
    )
    assert all(r.status_code == 200 for r in checks)
    assert {r.json()["run"]["id"] for r in checks} == {body["run"]["id"]}
    assert (
        await a.client.post(base + "/pause", json={"expected_version": 1}, headers=key())
    ).status_code == 200
    assert (await a.client.post(base + "/checks", json={}, headers=key())).status_code == 409
    stale = await a.client.patch(
        base, json={"include_prereleases": True, "expected_version": 1}, headers=key()
    )
    assert stale.status_code == 409 and stale.json()["error"]["latest"]["version"] == 2
    assert (
        await a.client.post(base + "/resume", json={"expected_version": 2}, headers=key())
    ).status_code == 200
    async with harness.db.begin() as conn:
        await conn.execute(
            update(collection_runs)
            .where(collection_runs.c.id == UUID(body["run"]["id"]))
            .values(state="succeeded")
        )
    cooldown = await a.client.post(base + "/checks", json={}, headers=key())
    assert cooldown.status_code == 429, cooldown.text
    assert int(cooldown.headers["retry-after"]) >= 1
    assert cooldown.json()["error"]["retry_at"]


async def test_provider_failure_and_csrf_do_not_save(harness):
    provider = install_provider(harness)
    a = await harness.client_for()
    p = await project(a.client)
    response = await add(a.client, p["id"], headers=key() | {"Origin": "https://attacker.example"})
    assert response.status_code == 403
    provider.validate_repository.assert_not_awaited()
    provider.validate_repository.side_effect = CollectionError("source_unavailable", status=422)
    failed = await add(a.client, p["id"])
    assert failed.status_code == 422
    assert (await a.client.get(f"/api/v1/projects/{p['id']}/subscriptions")).json()["total"] == 0


async def test_release_history_filter_paging_and_attachment_authorization(harness):
    install_provider(harness)
    a = await harness.client_for()
    p = await project(a.client)
    one = (await add(a.client, p["id"], "owner/repo1")).json()["subscription"]
    two = (await add(a.client, p["id"], "owner/repo2")).json()["subscription"]
    now = datetime.now(UTC)
    ids = []
    async with harness.db.begin() as conn:
        for i, title in enumerate(("literal%one", "literal_two", "ordinary")):
            rid = uuid4()
            ids.append(rid)
            await conn.execute(
                insert(releases).values(
                    id=rid,
                    source_id=UUID(one["source"]["id"]),
                    github_id=i + 1,
                    title=title,
                    tag_name=f"v{i}",
                    body="plain text <script>alert(1)</script>",
                    url=f"https://github.com/owner/repo1/releases/tag/v{i}",
                    published_at=now - timedelta(days=i),
                    prerelease=i == 1,
                    content_hash=str(i) * 64,
                )
            )
    base = f"/api/v1/subscriptions/{one['id']}/releases"
    result = (await a.client.get(base + "?page_size=1")).json()
    assert result["total"] == 3 and result["has_next"] and len(result["items"]) == 1
    assert "body" not in result["items"][0]
    assert (await a.client.get(base, params={"q": "%"})).json()["total"] == 1
    assert (await a.client.get(base, params={"q": "_"})).json()["total"] == 1
    assert (await a.client.get(base + "?prerelease=true")).json()["total"] == 1
    assert (await a.client.get(base + "?page=5&page_size=1")).json()["total"] == 3
    detail = (await a.client.get(base + f"/{ids[0]}")).json()
    assert detail["account_id"] == str(a.owner) and "<script>" in detail["release"]["body"]
    async with harness.db.begin() as conn:
        for ordinal in (1, 2):
            await conn.execute(
                insert(release_revisions).values(
                    id=uuid4(),
                    release_id=ids[0],
                    revision=ordinal,
                    content_hash=str(ordinal) * 64,
                    title="Revision",
                    tag_name="v0",
                    body=f"Version {ordinal}",
                    url="https://github.com/owner/repo1/releases/tag/v0",
                    published_at=now,
                    prerelease=False,
                    observed_at=now,
                )
            )
    detail = (await a.client.get(base + f"/{ids[0]}")).json()
    assert [r["revision"] for r in detail["revisions"]] == [2, 1]
    assert (
        await a.client.get(f"/api/v1/subscriptions/{two['id']}/releases/{ids[0]}")
    ).status_code == 404
    assert (
        await a.client.post(
            f"/api/v1/subscriptions/{one['id']}/remove", json={"expected_version": 1}, headers=key()
        )
    ).status_code == 200
    assert (await a.client.get(base)).json()["total"] == 3


async def test_readd_preserves_bootstrap_and_cannot_bypass_cooldown(harness):
    install_provider(harness)
    a = await harness.client_for()
    p = await project(a.client)
    body = (await add(a.client, p["id"])).json()
    sub = body["subscription"]
    async with harness.db.begin() as conn:
        await conn.execute(
            update(subscriptions)
            .where(subscriptions.c.id == UUID(sub["id"]))
            .values(bootstrap_state="complete", bootstrap_release_ids=[1001, 1002])
        )
        await conn.execute(
            update(collection_runs)
            .where(collection_runs.c.id == UUID(body["run"]["id"]))
            .values(state="succeeded")
        )
        original = (
            (await conn.execute(select(subscriptions).where(subscriptions.c.id == UUID(sub["id"]))))
            .mappings()
            .one()
        )
    assert (
        await a.client.post(
            f"/api/v1/subscriptions/{sub['id']}/remove", json={"expected_version": 1}, headers=key()
        )
    ).status_code == 200
    assert (await add(a.client, p["id"])).status_code == 429
    async with harness.db.begin() as conn:
        await conn.execute(
            update(sources)
            .where(sources.c.id == UUID(sub["source"]["id"]))
            .values(last_enqueued_at=datetime.now(UTC) - timedelta(seconds=61))
        )
    readded = await add(a.client, p["id"])
    assert readded.status_code == 200, readded.text
    async with harness.db.connect() as conn:
        current = (
            (await conn.execute(select(subscriptions).where(subscriptions.c.id == UUID(sub["id"]))))
            .mappings()
            .one()
        )
    assert current["bootstrap_state"] == "complete"
    assert current["bootstrap_release_ids"] == [1001, 1002]
    assert current["activated_at"] == original["activated_at"]


async def test_projection_preserves_partial_warning_while_next_run_queued(harness):
    install_provider(harness)
    a = await harness.client_for()
    p = await project(a.client)
    body = (await add(a.client, p["id"])).json()
    sub = body["subscription"]
    async with harness.db.begin() as conn:
        await conn.execute(
            update(collection_runs)
            .where(collection_runs.c.id == UUID(body["run"]["id"]))
            .values(state="partial", coverage="bounded_30")
        )
        await conn.execute(
            update(subscriptions)
            .where(subscriptions.c.id == UUID(sub["id"]))
            .values(bootstrap_state="complete")
        )
        await conn.execute(
            insert(collection_runs).values(id=uuid4(), source_id=UUID(sub["source"]["id"]))
        )
    read = (await a.client.get(f"/api/v1/subscriptions/{sub['id']}")).json()
    assert read["connection_state"] == "attention" and read["active_run_id"]
    assert read["last_success_at"] is None


async def test_syntax_and_durable_validation_throttle(harness):
    provider = install_provider(harness)
    a = await harness.client_for()
    p = await project(a.client)
    assert (await add(a.client, p["id"], "https://attacker.example/repo")).status_code == 422
    provider.validate_repository.assert_not_awaited()
    provider.validate_repository.side_effect = CollectionError("provider_unavailable")
    for _ in range(20):
        assert (await add(a.client, p["id"])).status_code == 502
    blocked = await add(a.client, p["id"])
    assert blocked.status_code == 429 and provider.validate_repository.await_count == 20
