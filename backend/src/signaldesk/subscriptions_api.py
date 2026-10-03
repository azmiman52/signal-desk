"""Owner-scoped subscription management and retained public-source history."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from signaldesk.api import Input, Version, engine, owned_project
from signaldesk.collection import enqueue_run, run_projection
from signaldesk.db import (
    collection_runs,
    idempotency_keys,
    projects,
    release_revisions,
    releases,
    sources,
    subscriptions,
)
from signaldesk.errors import APIError
from signaldesk.github_releases import repository_name
from signaldesk.security import authenticate, digest, limit
from signaldesk.subscription_rules import ensure_source_capacity

router = APIRouter(prefix="/api/v1")


class SubscriptionCreate(Input):
    repository: str = Field(min_length=1, max_length=300)
    include_prereleases: bool = Field(default=False, strict=True)


class SubscriptionPatch(Version):
    include_prereleases: bool = Field(strict=True)


class Empty(Input):
    pass


def key_for(request, *, required=True):
    value = request.headers.get("idempotency-key")
    if not value and not required:
        return None
    try:
        return UUID(value or "")
    except ValueError as exc:
        raise APIError(422, "validation_error", "A UUID Idempotency-Key is required.") from exc


def fingerprint(body):
    return digest(json.dumps(body.model_dump(exclude_unset=True), sort_keys=True))


async def replay(conn, owner, operation, key, request_hash, now):
    if key is None:
        return None
    condition = (
        idempotency_keys.c.account_id == owner,
        idempotency_keys.c.operation == operation,
        idempotency_keys.c.key == key,
    )
    record = (await conn.execute(select(idempotency_keys).where(*condition))).mappings().first()
    if not record:
        return None
    if record["expires_at"] <= now:
        await conn.execute(delete(idempotency_keys).where(*condition))
        return None
    if record["request_hash"] != request_hash:
        raise APIError(409, "idempotency_key_reused", "Use a new key for a different request.")
    return record


def replay_response(record):
    return JSONResponse(
        record["response"], status_code=record["status"], headers={"Idempotency-Replayed": "true"}
    )


async def remember(conn, owner, operation, key, request_hash, now, result, status):
    result = jsonable_encoder(result)
    if key:
        await conn.execute(
            insert(idempotency_keys).values(
                account_id=owner,
                operation=operation,
                key=key,
                request_hash=request_hash,
                response=result,
                status=status,
                expires_at=now + timedelta(days=7),
            )
        )
    return JSONResponse(result, status_code=status)


def subscription_query():
    latest = (
        select(collection_runs.c.id, collection_runs.c.state)
        .where(collection_runs.c.source_id == subscriptions.c.source_id)
        .order_by(collection_runs.c.created_at.desc(), collection_runs.c.id.desc())
        .limit(1)
        .lateral()
    )
    terminal = (
        select(collection_runs.c.state)
        .where(
            collection_runs.c.source_id == subscriptions.c.source_id,
            collection_runs.c.state.in_(["succeeded", "partial", "failed"]),
        )
        .order_by(collection_runs.c.created_at.desc(), collection_runs.c.id.desc())
        .limit(1)
        .lateral()
    )
    return select(
        subscriptions,
        sources.c.repository,
        sources.c.url.label("source_url"),
        sources.c.last_success_at,
        sources.c.last_check_at,
        latest.c.id.label("latest_run_id"),
        latest.c.state.label("latest_run_state"),
        terminal.c.state.label("terminal_run_state"),
    ).select_from(
        subscriptions.join(sources)
        .outerjoin(latest, text("true"))
        .outerjoin(terminal, text("true"))
    )


def subscription_json(row, owner):
    state = (
        "connected"
        if row["bootstrap_state"] == "complete" and row["last_success_at"]
        else "pending"
    )
    if row["terminal_run_state"] in {"partial", "failed"}:
        state = "attention"
    active = row["latest_run_state"] in {"queued", "running", "retry_wait"}
    return jsonable_encoder(
        {
            "id": row["id"],
            "account_id": owner,
            "project_id": row["project_id"],
            "source": {
                "id": row["source_id"],
                "repository": row["repository"],
                "url": row["source_url"],
            },
            "state": row["state"],
            "include_prereleases": row["include_prereleases"],
            "version": row["version"],
            "bootstrap_state": row["bootstrap_state"],
            "connection_state": state,
            "last_success_at": row["last_success_at"],
            "last_check_at": row["last_check_at"],
            "next_check_at": None,
            "active_run_id": row["latest_run_id"] if active else None,
        }
    )


async def owned_subscription(conn, owner, subscription_id):
    # Authorize via project before exposing even shared-source run/release IDs.
    row = (
        (
            await conn.execute(
                subscription_query()
                .join(projects, subscriptions.c.project_id == projects.c.id)
                .where(subscriptions.c.id == subscription_id, projects.c.account_id == owner)
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise APIError(404, "not_found", "Source subscription not found.")
    project = await owned_project(conn, owner, row["project_id"])
    await conn.execute(
        select(subscriptions.c.id).where(subscriptions.c.id == subscription_id).with_for_update()
    )
    return row, project


def active_project(project):
    if project["archived_at"]:
        raise APIError(409, "invalid_transition", "Restore this project before changing sources.")


async def page_rows(conn, statement, owner, now, page, page_size, *, order_by):
    filtered = statement.cte("filtered")
    order = [getattr(filtered.c, name).desc() for name in order_by]
    paged = (
        select(filtered)
        .order_by(*order)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .cte("paged")
    )
    total = select(func.count().label("total")).select_from(filtered).cte("total")
    rows = (
        (
            await conn.execute(
                select(total.c.total, *paged.c)
                .select_from(total.outerjoin(paged, text("true")))
                .order_by(*[getattr(paged.c, name).desc() for name in order_by])
            )
        )
        .mappings()
        .all()
    )
    count = rows[0]["total"]
    return {
        "account_id": str(owner),
        "items": [r for r in rows if r["id"]],
        "total": count,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < count,
        "as_of": now,
    }


@router.get("/projects/{project_id}/subscriptions")
async def list_subscriptions(
    request: Request,
    project_id: UUID,
    include_removed: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    async with engine(request).begin() as conn:
        account, _, now = await authenticate(request, conn)
        await owned_project(conn, account["id"], project_id)
        query = subscription_query().where(subscriptions.c.project_id == project_id)
        if not include_removed:
            query = query.where(subscriptions.c.state != "removed")
        result = await page_rows(
            conn, query, account["id"], now, page, page_size, order_by=("created_at", "id")
        )
        result["items"] = [subscription_json(r, account["id"]) for r in result["items"]]
        return result


@router.get("/subscriptions/{subscription_id}")
async def get_subscription(request: Request, subscription_id: UUID):
    async with engine(request).begin() as conn:
        account, _, _ = await authenticate(request, conn)
        row, _ = await owned_subscription(conn, account["id"], subscription_id)
        return subscription_json(row, account["id"])


async def add_preflight(request, conn, project_id, body, key):
    account, _, now = await authenticate(request, conn, write=True)
    project = await owned_project(conn, account["id"], project_id)
    active_project(project)
    operation = f"subscription:add:{project_id}"
    record = await replay(conn, account["id"], operation, key, fingerprint(body), now)
    if record:
        row, _ = await owned_subscription(
            conn, account["id"], UUID(record["response"]["subscription"]["id"])
        )
        if row["state"] != "active":
            raise APIError(409, "invalid_transition", "The saved subscription is no longer active.")
    return account, now, operation, record


@router.post("/projects/{project_id}/subscriptions", status_code=201)
async def add_subscription(request: Request, project_id: UUID, body: SubscriptionCreate):
    key = key_for(request)
    async with engine(request).begin() as conn:
        account, _, _, record = await add_preflight(request, conn, project_id, body, key)
        if record:
            return replay_response(record)
        repository_name(body.repository)
        try:
            await limit(conn, "repository:owner:" + str(account["id"]), 20)
        except APIError as exc:
            if exc.status == 429:
                raise APIError(
                    429,
                    "rate_limited",
                    "Too many repository checks. Try again shortly.",
                    headers={"Retry-After": "60"},
                ) from exc
            raise
    # No private locks/transactions survive the bounded provider call.
    metadata = await request.app.state.release_provider.validate_repository(body.repository)
    async with engine(request).begin() as conn:
        account, now, operation, record = await add_preflight(request, conn, project_id, body, key)
        if record:
            return replay_response(record)
        await conn.execute(
            pg_insert(sources)
            .values(id=uuid4(), **metadata)
            .on_conflict_do_nothing(index_elements=[sources.c.github_id])
        )
        source = (
            (
                await conn.execute(
                    select(sources).where(sources.c.github_id == metadata["github_id"])
                )
            )
            .mappings()
            .one()
        )
        old = (
            (
                await conn.execute(
                    select(subscriptions)
                    .where(
                        subscriptions.c.project_id == project_id,
                        subscriptions.c.source_id == source["id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if old and old["state"] != "removed":
            raise APIError(
                409, "duplicate_subscription", "This project already follows that repository."
            )
        await ensure_source_capacity(conn, account["id"], candidate_source=source["id"])
        if old:
            subscription_id = old["id"]
            await conn.execute(
                update(subscriptions)
                .where(subscriptions.c.id == subscription_id)
                .values(
                    state="active",
                    include_prereleases=body.include_prereleases,
                    version=old["version"] + 1,
                )
            )
        else:
            subscription_id = uuid4()
            await conn.execute(
                insert(subscriptions).values(
                    id=subscription_id,
                    project_id=project_id,
                    source_id=source["id"],
                    state="active",
                    include_prereleases=body.include_prereleases,
                    version=1,
                    bootstrap_state="pending",
                    activated_at=now,
                )
            )
        # Existing source metadata is updated only after account/project/subscription
        # locks. Reattachment preserves bootstrap selection and respects cooldown.
        await conn.execute(
            update(sources)
            .where(sources.c.id == source["id"])
            .values(repository=metadata["repository"], url=metadata["url"])
        )
        run, reused = await enqueue_run(conn, source["id"], force=not bool(old))
        row = (
            (await conn.execute(subscription_query().where(subscriptions.c.id == subscription_id)))
            .mappings()
            .one()
        )
        result = {
            "account_id": str(account["id"]),
            "subscription": subscription_json(row, account["id"]),
            "run": run_projection(run),
            "reused": reused,
            "restored": bool(old),
        }
        return await remember(
            conn, account["id"], operation, key, fingerprint(body), now, result, 200 if old else 201
        )


async def mutate_subscription(request, subscription_id, body, action):
    key = key_for(request, required=action != "edit")
    operation = f"subscription:{action}:{subscription_id}"
    async with engine(request).begin() as conn:
        account, _, now = await authenticate(request, conn, write=True)
        row, project = await owned_subscription(conn, account["id"], subscription_id)
        active_project(project)
        if row["state"] == "removed" and action != "remove":
            raise APIError(409, "invalid_transition", "Re-add the removed source to change it.")
        record = await replay(conn, account["id"], operation, key, fingerprint(body), now)
        if record:
            return replay_response(record)
        if row["version"] != body.expected_version:
            raise APIError(
                409,
                "version_conflict",
                "This source changed. Reload before saving.",
                latest=subscription_json(row, account["id"]),
            )
        values = {"version": row["version"] + 1}
        if action == "edit":
            values["include_prereleases"] = body.include_prereleases
        else:
            required = {"pause": "active", "resume": "paused"}
            if (action in required and row["state"] != required[action]) or row[
                "state"
            ] == "removed":
                raise APIError(
                    409, "invalid_transition", "That source action is no longer available."
                )
            values["state"] = {"pause": "paused", "resume": "active", "remove": "removed"}[action]
            if action == "resume":
                await ensure_source_capacity(conn, account["id"], candidate_source=row["source_id"])
        await conn.execute(
            update(subscriptions).where(subscriptions.c.id == subscription_id).values(**values)
        )
        if action == "resume" and row["bootstrap_state"] == "pending":
            await enqueue_run(conn, row["source_id"])
        updated = (
            (await conn.execute(subscription_query().where(subscriptions.c.id == subscription_id)))
            .mappings()
            .one()
        )
        return await remember(
            conn,
            account["id"],
            operation,
            key,
            fingerprint(body),
            now,
            subscription_json(updated, account["id"]),
            200,
        )


@router.patch("/subscriptions/{subscription_id}")
async def edit_subscription(request: Request, subscription_id: UUID, body: SubscriptionPatch):
    return await mutate_subscription(request, subscription_id, body, "edit")


@router.post("/subscriptions/{subscription_id}/pause")
async def pause_subscription(request: Request, subscription_id: UUID, body: Version):
    return await mutate_subscription(request, subscription_id, body, "pause")


@router.post("/subscriptions/{subscription_id}/resume")
async def resume_subscription(request: Request, subscription_id: UUID, body: Version):
    return await mutate_subscription(request, subscription_id, body, "resume")


@router.post("/subscriptions/{subscription_id}/remove")
async def remove_subscription(request: Request, subscription_id: UUID, body: Version):
    return await mutate_subscription(request, subscription_id, body, "remove")


@router.post("/subscriptions/{subscription_id}/checks")
async def check_subscription(request: Request, subscription_id: UUID, body: Empty):
    key = key_for(request)
    operation = f"subscription:check:{subscription_id}"
    async with engine(request).begin() as conn:
        account, _, now = await authenticate(request, conn, write=True)
        row, project = await owned_subscription(conn, account["id"], subscription_id)
        active_project(project)
        if row["state"] != "active":
            raise APIError(409, "invalid_transition", "Resume this source before checking it.")
        record = await replay(conn, account["id"], operation, key, fingerprint(body), now)
        if record:
            return replay_response(record)
        run, reused = await enqueue_run(conn, row["source_id"])
        result = {"account_id": str(account["id"]), "run": run_projection(run), "reused": reused}
        return await remember(
            conn,
            account["id"],
            operation,
            key,
            fingerprint(body),
            now,
            result,
            200 if reused else 202,
        )


@router.get("/subscriptions/{subscription_id}/runs")
async def list_runs(
    request: Request,
    subscription_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    async with engine(request).begin() as conn:
        account, _, now = await authenticate(request, conn)
        row, _ = await owned_subscription(conn, account["id"], subscription_id)
        query = select(collection_runs).where(collection_runs.c.source_id == row["source_id"])
        result = await page_rows(
            conn, query, account["id"], now, page, page_size, order_by=("created_at", "id")
        )
        result["items"] = [run_projection(r) for r in result["items"]]
        return result


@router.get("/subscriptions/{subscription_id}/runs/{run_id}")
async def get_run(request: Request, subscription_id: UUID, run_id: UUID):
    async with engine(request).begin() as conn:
        account, _, _ = await authenticate(request, conn)
        row, _ = await owned_subscription(conn, account["id"], subscription_id)
        run = (
            (
                await conn.execute(
                    select(collection_runs).where(
                        collection_runs.c.id == run_id,
                        collection_runs.c.source_id == row["source_id"],
                    )
                )
            )
            .mappings()
            .first()
        )
        if not run:
            raise APIError(404, "not_found", "Collection run not found.")
        return {"account_id": str(account["id"]), "run": run_projection(run)}


RELEASE_FIELDS = (
    "id",
    "source_id",
    "title",
    "tag_name",
    "url",
    "published_at",
    "prerelease",
    "first_seen_at",
    "last_seen_at",
)


@router.get("/subscriptions/{subscription_id}/releases")
async def list_releases(
    request: Request,
    subscription_id: UUID,
    q: str = Query("", max_length=200),
    prerelease: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    async with engine(request).begin() as conn:
        account, _, now = await authenticate(request, conn)
        row, _ = await owned_subscription(conn, account["id"], subscription_id)
        query = select(
            *[getattr(releases.c, f) for f in RELEASE_FIELDS],
            func.coalesce(releases.c.published_at, releases.c.first_seen_at).label("sort_at"),
        ).where(releases.c.source_id == row["source_id"])
        if prerelease is not None:
            query = query.where(releases.c.prerelease == prerelease)
        if q:
            literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{literal}%"
            query = query.where(
                or_(
                    *[
                        getattr(releases.c, f).ilike(pattern, escape="\\")
                        for f in ("title", "tag_name", "body")
                    ]
                )
            )
        result = await page_rows(
            conn, query, account["id"], now, page, page_size, order_by=("sort_at", "id")
        )
        result["items"] = [{f: r[f] for f in RELEASE_FIELDS} for r in result["items"]]
        return result


@router.get("/subscriptions/{subscription_id}/releases/{release_id}")
async def get_release(request: Request, subscription_id: UUID, release_id: UUID):
    async with engine(request).begin() as conn:
        account, _, _ = await authenticate(request, conn)
        row, _ = await owned_subscription(conn, account["id"], subscription_id)
        release = (
            (
                await conn.execute(
                    select(releases).where(
                        releases.c.id == release_id, releases.c.source_id == row["source_id"]
                    )
                )
            )
            .mappings()
            .first()
        )
        if not release:
            raise APIError(404, "not_found", "Release not found.")
        fields = (
            "id",
            "revision",
            "title",
            "tag_name",
            "body",
            "url",
            "published_at",
            "prerelease",
            "observed_at",
        )
        revisions = (
            (
                await conn.execute(
                    select(*[getattr(release_revisions.c, f) for f in fields])
                    .where(release_revisions.c.release_id == release_id)
                    .order_by(release_revisions.c.revision.desc())
                    .limit(51)
                )
            )
            .mappings()
            .all()
        )
        return {
            "account_id": str(account["id"]),
            "release": {f: release[f] for f in (*RELEASE_FIELDS, "body")},
            "revisions": [dict(r) for r in revisions[:50]],
            "revisions_truncated": len(revisions) > 50,
        }
