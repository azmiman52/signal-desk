import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update

from signaldesk.collection import CollectionWorker, enqueue_run, run_projection
from signaldesk.db import (
    accounts,
    collection_rejections,
    collection_runs,
    projects,
    release_revisions,
    releases,
    reviews,
    sources,
    subscriptions,
)
from signaldesk.github_releases import CollectionError, ReleasePage, normalize_release

pytestmark = pytest.mark.integration


def raw_release(identifier=1, **changes):
    return {
        "id": identifier,
        "draft": False,
        "prerelease": False,
        "tag_name": f"v{identifier}",
        "name": f"Release {identifier}",
        "body": "Notes",
        "html_url": f"https://github.com/example/repo/releases/tag/v{identifier}",
        "published_at": "2026-01-01T00:00:00Z",
        **changes,
    }


class FakeProvider:
    def __init__(self, entries=None, *, has_more=False, rejections=None):
        self.entries = entries if entries is not None else [normalize_release(raw_release())]
        self.has_more, self.rejections = has_more, rejections or []
        self.calls = 0
        self.before_return = None
        self.error = None

    async def fetch_releases(self, repository, github_id):
        self.calls += 1
        if self.before_return:
            await self.before_return()
        if self.error:
            raise self.error
        return ReleasePage(
            {
                "github_id": github_id,
                "repository": repository,
                "url": "https://github.com/" + repository,
            },
            self.entries,
            self.rejections,
            len(self.entries) + len(self.rejections),
            self.has_more,
        )


async def seed(database, *, source_id=None, include_prereleases=False):
    owner, project, subscription = uuid4(), uuid4(), uuid4()
    async with database.begin() as conn:
        await conn.execute(
            insert(accounts).values(
                id=owner, github_user_id=int(uuid4().hex[:12], 16), display_name="Owner"
            )
        )
        await conn.execute(insert(projects).values(id=project, account_id=owner, name="App"))
        if source_id is None:
            source_id = uuid4()
            await conn.execute(
                insert(sources).values(
                    id=source_id,
                    github_id=1,
                    repository="example/repo",
                    url="https://github.com/example/repo",
                )
            )
        await conn.execute(
            insert(subscriptions).values(
                id=subscription,
                project_id=project,
                source_id=source_id,
                include_prereleases=include_prereleases,
            )
        )
    return owner, project, subscription, source_id


async def enqueue(database, source_id, force=False):
    async with database.begin() as conn:
        return await enqueue_run(conn, source_id, force=force)


async def rows(database, table):
    async with database.connect() as conn:
        return (await conn.execute(select(table))).mappings().all()


async def test_durable_coalescing_and_cooldown(database):
    *_, source = await seed(database)
    results = await asyncio.gather(*(enqueue(database, source) for _ in range(12)))
    assert len({r[0]["id"] for r in results}) == 1
    assert sum(not r[1] for r in results) == 1
    assert await CollectionWorker(database, FakeProvider()).run_once()
    with pytest.raises(CollectionError, match="wait") as err:
        await enqueue(database, source)
    assert err.value.code == "cooldown" and err.value.retry_at
    assert not await CollectionWorker(database, FakeProvider()).run_once()


async def test_bootstrap_second_project_dedupe_and_revision(database):
    *_, source = await seed(database)
    provider = FakeProvider()
    worker = CollectionWorker(database, provider)
    await enqueue(database, source)
    assert await worker.run_once()
    first = (await rows(database, reviews))[0]
    async with database.begin() as conn:
        await conn.execute(update(reviews).values(state="resolved", version=2))
    await seed(database, source_id=source)
    await enqueue(database, source, True)
    await worker.run_once()
    assert len(await rows(database, releases)) == 1
    assert len(await rows(database, reviews)) == 2
    assert all(s["bootstrap_state"] == "complete" for s in await rows(database, subscriptions))
    provider.entries = [normalize_release(raw_release(body="Updated notes"))]
    await enqueue(database, source, True)
    await worker.run_once()
    assert len(await rows(database, release_revisions)) == 2
    preserved = next(r for r in await rows(database, reviews) if r["id"] == first["id"])
    assert preserved["state"] == "resolved" and preserved["version"] == 2
    assert len(await rows(database, reviews)) == 2


async def test_cap_and_prereleases_kept_in_history(database):
    *_, source = await seed(database)
    provider = FakeProvider(
        [normalize_release(raw_release(i, prerelease=(i % 2 == 0))) for i in range(1, 31)],
        has_more=True,
    )
    run, _ = await enqueue(database, source)
    await CollectionWorker(database, provider).run_once()
    assert len(await rows(database, releases)) == 30
    assert len(await rows(database, reviews)) == 15
    stored = (await rows(database, collection_runs))[0]
    assert stored["id"] == run["id"] and stored["state"] == "partial"
    assert stored["error_code"] == "coverage_limited"
    assert (await rows(database, sources))[0]["last_success_at"] is None
    projection = run_projection(stored)
    assert projection["counts"]["discovered"] == 30 and projection["scan_limit"] == 30
    assert "lease_token" not in projection and "source_id" not in projection


@pytest.mark.parametrize("mode", ["paused", "removed", "archived"])
async def test_inactive_during_fetch_cannot_receive_reviews(database, mode):
    _, project, sub, source = await seed(database)
    provider = FakeProvider()

    async def change():
        # Locks acquired here prove the provider call holds no delivery locks.
        async with database.begin() as conn:
            if mode == "archived":
                await conn.execute(
                    update(projects).where(projects.c.id == project).values(archived_at=func.now())
                )
            else:
                await conn.execute(
                    update(subscriptions).where(subscriptions.c.id == sub).values(state=mode)
                )

    provider.before_return = change
    await enqueue(database, source)
    await CollectionWorker(database, provider).run_once()
    assert not await rows(database, reviews)
    assert (await rows(database, collection_runs))[0]["state"] == "cancelled"


async def test_one_paused_project_does_not_stop_another(database):
    _, _, sub, source = await seed(database)
    _, other_project, _, _ = await seed(database, source_id=source)
    async with database.begin() as conn:
        await conn.execute(
            update(subscriptions).where(subscriptions.c.id == sub).values(state="paused")
        )
    await enqueue(database, source)
    await CollectionWorker(database, FakeProvider()).run_once()
    assert [r["project_id"] for r in await rows(database, reviews)] == [other_project]


async def test_lease_loss_fences_stale_delivery_and_manual_retry(database):
    *_, source = await seed(database)
    provider = FakeProvider()
    worker = CollectionWorker(database, provider)
    await enqueue(database, source)
    claimed = await worker._claim()
    page = await provider.fetch_releases("example/repo", 1)
    async with database.begin() as conn:
        await conn.execute(
            update(collection_runs).values(
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    retry, reused = await enqueue(database, source, True)
    assert not reused and retry["id"] != claimed["id"]
    assert not await worker._deliver(claimed, page)
    assert not await rows(database, releases)
    await worker.run_once()
    assert len(await rows(database, releases)) == 1
    assert {r["state"] for r in await rows(database, collection_runs)} == {"failed", "succeeded"}


async def test_malformed_partial_and_retry_does_not_duplicate(database):
    *_, source = await seed(database)
    provider = FakeProvider(
        rejections=[{"position": 1, "reason": "invalid_release", "payload_hash": "0" * 64}]
    )
    await enqueue(database, source)
    worker = CollectionWorker(database, provider)
    await worker.run_once()
    assert (await rows(database, collection_runs))[0]["state"] == "partial"
    assert len(await rows(database, collection_rejections)) == 1
    assert (await rows(database, subscriptions))[0]["bootstrap_state"] == "pending"
    provider.rejections = []
    await enqueue(database, source, True)
    await worker.run_once()
    assert len(await rows(database, reviews)) == 1
    assert (await rows(database, subscriptions))[0]["bootstrap_state"] == "complete"
    assert (await rows(database, sources))[0]["last_success_at"] is not None


async def test_empty_success_and_error_preserves_freshness(database):
    *_, source = await seed(database)
    provider = FakeProvider([])
    worker = CollectionWorker(database, provider)
    await enqueue(database, source)
    await worker.run_once()
    success = (await rows(database, sources))[0]["last_success_at"]
    assert success
    provider.error = CollectionError(
        "rate_limited", retry_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await enqueue(database, source, True)
    await worker.run_once()
    assert (await rows(database, sources))[0]["last_success_at"] == success
    failed = next(r for r in await rows(database, collection_runs) if r["state"] == "failed")
    assert failed["error_code"] == "rate_limited" and failed["retry_at"]


async def test_replay_after_atomic_commit_is_noop(database):
    *_, source = await seed(database)
    provider = FakeProvider()
    worker = CollectionWorker(database, provider)
    await enqueue(database, source)
    run = await worker._claim()
    page = await provider.fetch_releases("example/repo", 1)
    assert await worker._deliver(run, page)
    assert not await worker._deliver(run, page)
    assert len(await rows(database, reviews)) == 1


async def test_revision_reversion_retains_a_b_a(database):
    *_, source = await seed(database)
    provider = FakeProvider()
    worker = CollectionWorker(database, provider)
    for body in ["A", "B", "A", "A"]:
        provider.entries = [normalize_release(raw_release(body=body))]
        await enqueue(database, source, True)
        await worker.run_once()
    history = sorted(await rows(database, release_revisions), key=lambda r: r["revision"])
    assert [r["body"] for r in history] == ["A", "B", "A"]
    assert [r["revision"] for r in history] == [1, 2, 3]


async def test_partial_bootstrap_does_not_widen_selection(database):
    *_, source = await seed(database)
    provider = FakeProvider(
        rejections=[{"position": 1, "reason": "invalid_release", "payload_hash": "0" * 64}]
    )
    worker = CollectionWorker(database, provider)
    await enqueue(database, source)
    await worker.run_once()
    # A newly encountered old release is retained, not imported as initial history.
    provider.entries = [normalize_release(raw_release()), normalize_release(raw_release(2))]
    provider.rejections = []
    await enqueue(database, source, True)
    await worker.run_once()
    assert len(await rows(database, releases)) == 2
    assert len(await rows(database, reviews)) == 1
    assert (await rows(database, subscriptions))[0]["bootstrap_release_ids"] == [1]


async def test_late_subscriber_gets_followup_job(database):
    from sqlalchemy import event

    owner, _, _, source = await seed(database)
    provider = FakeProvider()
    worker = CollectionWorker(database, provider)
    await enqueue(database, source)
    run = await worker._claim()
    page = await provider.fetch_releases("example/repo", 1)
    selected = asyncio.Event()

    def observed(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(
            "SELECT subscriptions.id, subscriptions.project_id, projects.account_id"
        ):
            selected.set()

    event.listen(database.sync_engine, "after_cursor_execute", observed)
    try:
        async with database.begin() as blocker:
            await blocker.execute(select(accounts).where(accounts.c.id == owner).with_for_update())
            delivery = asyncio.create_task(worker._deliver(run, page))
            await asyncio.wait_for(selected.wait(), timeout=3)
            await seed(database, source_id=source)
            same, reused = await enqueue(database, source, True)
            assert reused and same["id"] == run["id"]
        assert await delivery
    finally:
        event.remove(database.sync_engine, "after_cursor_execute", observed)
    assert len(await rows(database, reviews)) == 1
    assert sum(r["state"] == "queued" for r in await rows(database, collection_runs)) == 1
    await worker.run_once()
    assert len(await rows(database, reviews)) == 2


async def test_page_failure_rolls_back_all_content_and_can_retry(database):
    *_, source = await seed(database)
    provider = FakeProvider(
        [normalize_release(raw_release()), {**normalize_release(raw_release(2)), "title": None}]
    )
    await enqueue(database, source)
    worker = CollectionWorker(database, provider)
    await worker.run_once()
    assert not await rows(database, releases)
    assert not await rows(database, reviews)
    assert (await rows(database, collection_runs))[0]["state"] == "failed"
    provider.entries = [normalize_release(raw_release()), normalize_release(raw_release(2))]
    await enqueue(database, source, True)
    await worker.run_once()
    assert len(await rows(database, reviews)) == 2
