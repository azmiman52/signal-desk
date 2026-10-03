"""Durable manual collection. Scheduling and automatic retries are intentionally deferred."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

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
from signaldesk.github_releases import ERROR_MESSAGES, CollectionError

ACTIVE_STATES = ("queued", "running")


def run_projection(row):
    return {
        "id": str(row["id"]),
        "state": row["state"],
        "attempt_count": row["attempt_count"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "retry_at": row["retry_at"],
        "counts": {
            "discovered": row["discovered_count"],
            "created": row["created_count"],
            "updated": row["updated_count"],
            "skipped": row["skipped_count"],
        },
        "coverage": row["coverage"],
        "continuation_scheduled": False,
        "scan_limit": 30,
        "error": {
            "code": row["error_code"],
            "message": ERROR_MESSAGES.get(row["error_code"], "Collection could not finish."),
        }
        if row["error_code"]
        else None,
    }


async def enqueue_run(conn, source_id, force=False):
    """Caller owns account/project/subscription locks; source is always locked last."""
    source = (
        (await conn.execute(select(sources).where(sources.c.id == source_id).with_for_update()))
        .mappings()
        .one()
    )
    now = await conn.scalar(select(func.clock_timestamp()))
    # Expired work must be recoverable even if no worker is currently running.
    await conn.execute(
        update(collection_runs)
        .where(
            collection_runs.c.source_id == source_id,
            collection_runs.c.state == "running",
            collection_runs.c.lease_expires_at <= now,
        )
        .values(
            state="failed",
            error_code="lease_expired",
            finished_at=now,
            lease_token=None,
            lease_expires_at=None,
        )
    )
    active = (
        (
            await conn.execute(
                select(collection_runs).where(
                    collection_runs.c.source_id == source_id,
                    collection_runs.c.state.in_(ACTIVE_STATES),
                )
            )
        )
        .mappings()
        .first()
    )
    if active:
        return active, True
    if not force and source["last_enqueued_at"]:
        retry_at = source["last_enqueued_at"] + timedelta(seconds=60)
        if retry_at > now:
            raise CollectionError("cooldown", status=429, retry_at=retry_at)
    run = (
        (
            await conn.execute(
                insert(collection_runs)
                .values(id=uuid4(), source_id=source_id, state="queued", created_at=now)
                .returning(collection_runs)
            )
        )
        .mappings()
        .one()
    )
    await conn.execute(
        update(sources).where(sources.c.id == source_id).values(last_enqueued_at=now)
    )
    return run, False


class CollectionWorker:
    def __init__(self, engine, provider):
        self.engine, self.provider = engine, provider

    async def _claim(self):
        async with self.engine.begin() as conn:
            now = await conn.scalar(select(func.clock_timestamp()))
            await conn.execute(
                update(collection_runs)
                .where(
                    collection_runs.c.state == "running", collection_runs.c.lease_expires_at <= now
                )
                .values(
                    state="failed",
                    error_code="lease_expired",
                    finished_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            row = (
                (
                    await conn.execute(
                        select(collection_runs)
                        .where(collection_runs.c.state == "queued")
                        .order_by(collection_runs.c.created_at, collection_runs.c.id)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            return (
                (
                    await conn.execute(
                        update(collection_runs)
                        .where(collection_runs.c.id == row["id"])
                        .values(
                            state="running",
                            attempt_count=collection_runs.c.attempt_count + 1,
                            started_at=now,
                            lease_token=uuid4(),
                            lease_expires_at=now + timedelta(seconds=120),
                        )
                        .returning(collection_runs)
                    )
                )
                .mappings()
                .one()
            )

    async def _fail(self, run, error):
        async with self.engine.begin() as conn:
            now = await conn.scalar(select(func.clock_timestamp()))
            await conn.execute(
                update(collection_runs)
                .where(
                    collection_runs.c.id == run["id"],
                    collection_runs.c.state == "running",
                    collection_runs.c.lease_token == run["lease_token"],
                    collection_runs.c.lease_expires_at > now,
                )
                .values(
                    state="failed",
                    finished_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code=error.code,
                    retry_at=error.retry_at,
                )
            )

    async def run_once(self):
        run = await self._claim()
        if run is None:
            return False
        try:
            async with self.engine.connect() as conn:
                source = (
                    (await conn.execute(select(sources).where(sources.c.id == run["source_id"])))
                    .mappings()
                    .one()
                )
                active = await conn.scalar(
                    select(subscriptions.c.id)
                    .join(projects)
                    .where(
                        subscriptions.c.source_id == source["id"],
                        subscriptions.c.state == "active",
                        projects.c.archived_at.is_(None),
                    )
                    .limit(1)
                )
            if not active:
                await self._deliver(run, None)
                return True
            page = await self.provider.fetch_releases(source["repository"], source["github_id"])
            await self._deliver(run, page)
        except CollectionError as exc:
            await self._fail(run, exc)
        except Exception:
            # Never persist/log provider response text, DB statements, or credentials.
            # A DB failure here leaves the fenced lease to expire for recovery.
            await self._fail(run, CollectionError("worker_failed"))
        return True

    async def _deliver(self, run, page):
        async with self.engine.begin() as conn:
            candidates = (
                (
                    await conn.execute(
                        select(
                            subscriptions.c.id, subscriptions.c.project_id, projects.c.account_id
                        )
                        .join(projects)
                        .where(subscriptions.c.source_id == run["source_id"])
                    )
                )
                .mappings()
                .all()
            )
            # Lock hierarchy shared with all API mutations: account -> project -> sub -> source.
            # A newly inserted subscriber not in this snapshot keeps bootstrap pending.
            for table, ids in (
                (accounts, {r["account_id"] for r in candidates}),
                (projects, {r["project_id"] for r in candidates}),
                (subscriptions, {r["id"] for r in candidates}),
            ):
                if ids:
                    await conn.execute(
                        select(table.c.id)
                        .where(table.c.id.in_(ids))
                        .order_by(table.c.id)
                        .with_for_update()
                    )
            await conn.execute(
                select(sources.c.id).where(sources.c.id == run["source_id"]).with_for_update()
            )
            current = (
                (
                    await conn.execute(
                        select(collection_runs)
                        .where(collection_runs.c.id == run["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = await conn.scalar(select(func.clock_timestamp()))
            if (
                current["state"] != "running"
                or current["lease_token"] != run["lease_token"]
                or current["lease_expires_at"] <= now
            ):
                return False
            ids = [r["id"] for r in candidates]
            recipients = (
                (
                    await conn.execute(
                        select(subscriptions)
                        .join(projects)
                        .where(
                            subscriptions.c.id.in_(ids),
                            subscriptions.c.state == "active",
                            projects.c.archived_at.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
                if ids
                else []
            )
            if not recipients or page is None:
                await conn.execute(
                    update(collection_runs)
                    .where(collection_runs.c.id == run["id"])
                    .values(
                        state="cancelled", finished_at=now, lease_token=None, lease_expires_at=None
                    )
                )
                await self._queue_late_bootstraps(
                    conn, run["source_id"], [] if page is None else ids
                )
                return True
            selections = {}
            for sub in recipients:
                selection = sub["bootstrap_release_ids"]
                if selection is None:
                    selection = (
                        page.selected_ids
                        if page.selected_ids is not None
                        else [entry["github_id"] for entry in page.entries]
                    )
                    await conn.execute(
                        update(subscriptions)
                        .where(subscriptions.c.id == sub["id"])
                        .values(bootstrap_release_ids=selection)
                    )
                selections[sub["id"]] = set(selection)
            created = revised = 0
            for value in page.entries:
                old = (
                    (
                        await conn.execute(
                            select(releases).where(
                                releases.c.source_id == run["source_id"],
                                releases.c.github_id == value["github_id"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                release_id = old["id"] if old else uuid4()
                if old is None:
                    created += 1
                    await conn.execute(
                        insert(releases).values(
                            id=release_id,
                            source_id=run["source_id"],
                            **value,
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                else:
                    revised += int(old["content_hash"] != value["content_hash"])
                    await conn.execute(
                        update(releases)
                        .where(releases.c.id == release_id)
                        .values(**value, last_seen_at=now)
                    )
                if old is None or old["content_hash"] != value["content_hash"]:
                    ordinal = (
                        await conn.scalar(
                            select(func.max(release_revisions.c.revision)).where(
                                release_revisions.c.release_id == release_id
                            )
                        )
                        or 0
                    ) + 1
                    revision = {k: v for k, v in value.items() if k != "github_id"}
                    await conn.execute(
                        insert(release_revisions).values(
                            id=uuid4(),
                            release_id=release_id,
                            revision=ordinal,
                            observed_at=now,
                            **revision,
                        )
                    )
                for sub in recipients:
                    initial = (
                        sub["bootstrap_state"] == "pending"
                        and value["github_id"] in selections[sub["id"]]
                    )
                    if value["prerelease"] and not sub["include_prereleases"]:
                        continue
                    if not initial and value["published_at"] < sub["activated_at"]:
                        continue
                    await conn.execute(
                        insert(reviews)
                        .values(
                            id=uuid4(),
                            project_id=sub["project_id"],
                            release_id=release_id,
                            initial_history=initial,
                            created_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=["project_id", "release_id"])
                    )
            for rejection in page.rejections:
                await conn.execute(
                    insert(collection_rejections)
                    .values(id=uuid4(), run_id=run["id"], **rejection)
                    .on_conflict_do_nothing(index_elements=["run_id", "position"])
                )
            if not page.rejections:
                await conn.execute(
                    update(subscriptions)
                    .where(
                        subscriptions.c.id.in_([r["id"] for r in recipients]),
                        subscriptions.c.bootstrap_state == "pending",
                    )
                    .values(bootstrap_state="complete")
                )
            partial = page.has_more or bool(page.rejections)
            error_code = (
                "malformed_entries"
                if page.rejections
                else ("coverage_limited" if page.has_more else None)
            )
            await conn.execute(
                update(sources)
                .where(sources.c.id == run["source_id"])
                .values(
                    repository=page.repository["repository"],
                    url=page.repository["url"],
                    last_check_at=now,
                    **({"last_success_at": now} if not partial else {}),
                )
            )
            await conn.execute(
                update(collection_runs)
                .where(collection_runs.c.id == run["id"])
                .values(
                    state="partial" if partial else "succeeded",
                    finished_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                    discovered_count=page.discovered,
                    created_count=created,
                    updated_count=revised,
                    skipped_count=len(page.rejections),
                    coverage="incomplete" if partial else "complete",
                    error_code=error_code,
                )
            )
            await self._queue_late_bootstraps(conn, run["source_id"], ids)
            return True

    @staticmethod
    async def _queue_late_bootstraps(conn, source_id, processed_ids):
        pending = await conn.scalar(
            select(subscriptions.c.id)
            .join(projects)
            .where(
                subscriptions.c.source_id == source_id,
                subscriptions.c.bootstrap_state == "pending",
                subscriptions.c.state == "active",
                projects.c.archived_at.is_(None),
                subscriptions.c.id.not_in(processed_ids),
            )
            .limit(1)
        )
        if pending:
            # The source lock serializes this against the API enqueue. This is fulfillment
            # of a coalesced bootstrap request, not an automatic provider retry/scheduler.
            await enqueue_run(conn, source_id, force=True)
