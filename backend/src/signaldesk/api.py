import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, insert, select, text, update

from signaldesk.db import (
    accounts,
    idempotency_keys,
    oauth_attempts,
    projects,
    rate_limits,
    sessions,
)
from signaldesk.errors import APIError
from signaldesk.security import account_json, authenticate, digest, limit
from signaldesk.subscription_rules import active_source_count, ensure_source_capacity

router = APIRouter(prefix="/api/v1")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectCreate(Input):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class Version(Input):
    expected_version: int = Field(ge=1, strict=True)


class ProjectPatch(Version):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def reject_empty(self):
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Name cannot be null")
        if not (self.model_fields_set & {"name", "description"}):
            raise ValueError("Provide a name or description")
        return self


class AccountPatch(Input):
    timezone: str = Field(max_length=100)

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone") from exc
        return value


def engine(request):
    if request.app.state.engine is None:
        raise APIError(503, "service_unavailable", "The database is not configured.")
    return request.app.state.engine


def project_json(project):
    return jsonable_encoder(
        {
            key: project[key]
            for key in ("id", "name", "description", "created_at", "archived_at", "version")
        }
    ) | {
        "active_source_count": 0
        if project["archived_at"]
        else project.get("active_source_count", 0)
    }


def login_error(request, exc):
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/sign-in?" + urlencode({"error": exc.code}), status_code=303)
    raise exc


@router.get("/auth/github/start")
async def oauth_start(request: Request, return_to: str = "/projects"):
    settings = request.app.state.settings
    if not settings.oauth_enabled:
        return login_error(
            request, APIError(503, "oauth_not_configured", "GitHub sign-in is not configured.")
        )
    if return_to not in {"/", "/projects"}:
        raise APIError(422, "validation_error", "Invalid return destination.")
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    async with engine(request).begin() as conn:
        # Global bound caps storage and works across API processes. Do not trust X-Forwarded-For.
        await limit(conn, "oauth:global", 200)
        await limit(
            conn, "oauth:ip:" + digest(request.client.host if request.client else "unknown"), 20
        )
        now = await conn.scalar(select(func.clock_timestamp()))
        await conn.execute(delete(oauth_attempts).where(oauth_attempts.c.expires_at < now))
        await conn.execute(
            delete(rate_limits).where(rate_limits.c.window_start < now - timedelta(days=1))
        )
        await conn.execute(
            insert(oauth_attempts).values(
                state_hash=digest(state),
                browser_hash=digest(browser),
                verifier=verifier,
                return_to=return_to,
                expires_at=now + timedelta(minutes=10),
            )
        )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    response = RedirectResponse(
        "https://github.com/login/oauth/authorize?"
        + urlencode(
            {
                "client_id": settings.github_client_id,
                "redirect_uri": settings.github_redirect_uri,
                "scope": "",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        ),
        status_code=302,
    )
    response.set_cookie(
        settings.oauth_cookie_name,
        browser,
        max_age=600,
        secure=settings.secure_cookie,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/github/callback")
async def oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    settings = request.app.state.settings
    try:
        if not settings.oauth_enabled:
            raise APIError(503, "oauth_not_configured", "GitHub sign-in is not configured.")
        if not 1 <= len(state) <= 128 or len(code) > 1024:
            raise APIError(400, "invalid_oauth_state", "Sign-in expired. Start again.")
        browser = request.cookies.get(settings.oauth_cookie_name, "")
        async with engine(request).begin() as conn:
            attempt = (
                (
                    await conn.execute(
                        select(oauth_attempts)
                        .where(oauth_attempts.c.state_hash == digest(state))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            now = await conn.scalar(select(func.clock_timestamp()))
            if (
                not attempt
                or not browser
                or len(browser) > 128
                or not secrets.compare_digest(attempt["browser_hash"], digest(browser))
                or attempt["expires_at"] <= now
            ):
                raise APIError(400, "invalid_oauth_state", "Sign-in expired. Start again.")
            await conn.execute(
                delete(oauth_attempts).where(oauth_attempts.c.state_hash == digest(state))
            )
        # State has committed as consumed before any outbound calls; retries start fresh.
        if error or not code:
            raise APIError(400, "oauth_denied", "GitHub sign-in was canceled.")
        provider_id, name = await request.app.state.oauth_provider.identity(
            settings, code, attempt["verifier"]
        )
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with engine(request).begin() as conn:
            # Account admission/upsert serializes across API processes, independent of sessions.
            await conn.execute(text("SELECT pg_advisory_xact_lock(71302201)"))
            account = (
                (
                    await conn.execute(
                        select(accounts)
                        .where(accounts.c.github_user_id == provider_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not account:
                if (
                    await conn.scalar(select(func.count()).select_from(accounts))
                    >= settings.max_accounts
                ):
                    raise APIError(409, "capacity_exceeded", "The pilot is currently full.")
                account = (
                    (
                        await conn.execute(
                            insert(accounts)
                            .values(id=uuid4(), github_user_id=provider_id, display_name=name)
                            .returning(accounts)
                        )
                    )
                    .mappings()
                    .one()
                )
            now = await conn.scalar(select(func.clock_timestamp()))
            # Rotate this browser's previous session even when switching accounts.
            # OAuth admission's global lock serializes these cross-account switches.
            previous = request.cookies.get(settings.cookie_name, "")
            if previous:
                previous_owner = await conn.scalar(
                    select(sessions.c.account_id).where(sessions.c.token_hash == digest(previous))
                )
                if previous_owner and previous_owner != account["id"]:
                    await conn.execute(
                        select(accounts.c.id)
                        .where(accounts.c.id == previous_owner)
                        .with_for_update()
                    )
                await conn.execute(
                    update(sessions)
                    .where(
                        sessions.c.token_hash == digest(previous),
                    )
                    .values(revoked_at=now)
                )
            expires = now + timedelta(days=7)
            await conn.execute(
                insert(sessions).values(
                    id=uuid4(),
                    account_id=account["id"],
                    token_hash=digest(token),
                    csrf_token=csrf,
                    created_at=now,
                    last_seen_at=now,
                    expires_at=expires,
                )
            )
        response = RedirectResponse(attempt["return_to"], status_code=303)
        response.set_cookie(
            settings.cookie_name,
            token,
            max_age=604800,
            secure=settings.secure_cookie,
            httponly=True,
            samesite="lax",
            path="/",
        )
    except APIError as exc:
        response = RedirectResponse("/sign-in?" + urlencode({"error": exc.code}), status_code=303)
    response.delete_cookie(
        settings.oauth_cookie_name,
        path="/",
        secure=settings.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/session")
async def get_session(request: Request):
    async with engine(request).begin() as conn:
        account, session, _ = await authenticate(request, conn)
        return {
            "account": account_json(account),
            "csrf_token": session["csrf_token"],
            "expires_at": session["expires_at"],
        }


@router.post("/auth/logout", status_code=204)
@router.post("/auth/logout-all", status_code=204)
async def logout(request: Request):
    async with engine(request).begin() as conn:
        account, session, now = await authenticate(request, conn, write=True)
        condition = (
            sessions.c.account_id == account["id"]
            if request.url.path.endswith("logout-all")
            else sessions.c.id == session["id"]
        )
        await conn.execute(update(sessions).where(condition).values(revoked_at=now))
    settings = request.app.state.settings
    response = Response(status_code=204)
    response.delete_cookie(
        settings.cookie_name, path="/", secure=settings.secure_cookie, httponly=True, samesite="lax"
    )
    return response


@router.patch("/account")
async def patch_account(request: Request, body: AccountPatch):
    async with engine(request).begin() as conn:
        account, _, _ = await authenticate(request, conn, write=True)
        updated = (
            (
                await conn.execute(
                    update(accounts)
                    .where(accounts.c.id == account["id"])
                    .values(timezone=body.timezone)
                    .returning(accounts)
                )
            )
            .mappings()
            .one()
        )
        return account_json(updated)


@router.get("/projects")
async def list_projects(
    request: Request,
    include_archived: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    async with engine(request).begin() as conn:
        account, _, now = await authenticate(request, conn)
        filters = [projects.c.account_id == account["id"]]
        if not include_archived:
            filters.append(projects.c.archived_at.is_(None))
        # One statement snapshot for both total and page, including an empty page.
        filtered = (
            select(projects, active_source_count(projects.c.id).label("active_source_count"))
            .where(*filters)
            .cte("filtered")
        )
        paged = (
            select(filtered)
            .order_by(filtered.c.created_at.desc(), filtered.c.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
            .cte("paged")
        )
        total = select(func.count().label("total")).select_from(filtered).cte("totals")
        rows = (
            (
                await conn.execute(
                    select(total.c.total, *paged.c)
                    .select_from(total.outerjoin(paged, text("true")))
                    .order_by(paged.c.created_at.desc(), paged.c.id.desc())
                )
            )
            .mappings()
            .all()
        )
        count = rows[0]["total"]
        return {
            "account_id": str(account["id"]),
            "items": [project_json(row) for row in rows if row["id"]],
            "total": count,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < count,
            "as_of": now,
        }


async def owned_project(conn, owner, project_id):
    project = (
        (
            await conn.execute(
                select(projects, active_source_count(projects.c.id).label("active_source_count"))
                .where(projects.c.id == project_id, projects.c.account_id == owner)
                .with_for_update(of=projects)
            )
        )
        .mappings()
        .first()
    )
    if not project:
        raise APIError(404, "not_found", "Project not found.")
    return project


@router.get("/projects/{project_id}")
async def get_project(request: Request, project_id: UUID):
    async with engine(request).begin() as conn:
        account, _, _ = await authenticate(request, conn)
        return project_json(await owned_project(conn, account["id"], project_id))


async def mutate_project(request, body, project_id=None, action="create"):
    raw_key = request.headers.get("idempotency-key")
    if not raw_key and action != "edit":
        raise APIError(422, "validation_error", "Idempotency-Key is required.")
    try:
        key = UUID(raw_key) if raw_key else None
    except ValueError as exc:
        raise APIError(422, "validation_error", "Idempotency-Key must be a UUID.") from exc
    payload = body.model_dump(exclude_unset=True)
    fingerprint = digest(json.dumps(payload, sort_keys=True))
    operation = f"project:{action}:{project_id or ''}"
    async with engine(request).begin() as conn:
        account, _, now = await authenticate(request, conn, write=True)
        owner = account["id"]
        project = await owned_project(conn, owner, project_id) if project_id else None
        if project and project["archived_at"] and action not in {"archive", "restore"}:
            raise APIError(409, "invalid_transition", "Restore this project before editing it.")
        where_key = (
            idempotency_keys.c.account_id == owner,
            idempotency_keys.c.operation == operation,
            idempotency_keys.c.key == key,
        )
        if key:
            record = (
                (await conn.execute(select(idempotency_keys).where(*where_key))).mappings().first()
            )
            if record and record["expires_at"] > now:
                if record["request_hash"] != fingerprint:
                    raise APIError(
                        409, "idempotency_key_reused", "Use a new key for a different request."
                    )
                cached_project = await owned_project(conn, owner, UUID(record["response"]["id"]))
                if cached_project["archived_at"] and action == "create":
                    raise APIError(
                        409, "invalid_transition", "The previously created project is archived."
                    )
                return JSONResponse(
                    record["response"],
                    status_code=record["status"],
                    headers={"Idempotency-Replayed": "true"},
                )
            if record:
                await conn.execute(delete(idempotency_keys).where(*where_key))
        if project and project["version"] != body.expected_version:
            raise APIError(
                409,
                "version_conflict",
                "This project changed. Reload before saving.",
                latest=project_json(project),
            )
        if action in {"create", "restore"}:
            if action == "restore" and project["archived_at"] is None:
                raise APIError(409, "invalid_transition", "The project is already active.")
            active = await conn.scalar(
                select(func.count())
                .select_from(projects)
                .where(projects.c.account_id == owner, projects.c.archived_at.is_(None))
            )
            if active >= 3:
                raise APIError(
                    409,
                    "capacity_exceeded",
                    "Archive a project before adding another. Limit: 3 active projects.",
                )
            if action == "restore":
                await ensure_source_capacity(conn, owner, restore_project=project_id)
        if action == "create":
            result = (
                (
                    await conn.execute(
                        insert(projects)
                        .values(
                            id=uuid4(),
                            account_id=owner,
                            name=body.name,
                            description=body.description,
                        )
                        .returning(projects)
                    )
                )
                .mappings()
                .one()
            )
        else:
            values = {"version": project["version"] + 1}
            if action == "edit":
                values.update(
                    {
                        k: (v if v is not None else "")
                        for k, v in payload.items()
                        if k != "expected_version"
                    }
                )
            elif action == "archive":
                if project["archived_at"]:
                    raise APIError(409, "invalid_transition", "The project is already archived.")
                values["archived_at"] = now
            else:
                values["archived_at"] = None
            result = (
                (
                    await conn.execute(
                        update(projects)
                        .where(projects.c.id == project_id)
                        .values(**values)
                        .returning(projects)
                    )
                )
                .mappings()
                .one()
            )
        result = await owned_project(conn, owner, result["id"])
        response, status = project_json(result), 201 if action == "create" else 200
        if key:
            await conn.execute(
                insert(idempotency_keys).values(
                    account_id=owner,
                    operation=operation,
                    key=key,
                    request_hash=fingerprint,
                    response=response,
                    status=status,
                    expires_at=now + timedelta(days=7),
                )
            )
        return JSONResponse(response, status_code=status)


@router.post("/projects", status_code=201)
async def create_project(request: Request, body: ProjectCreate):
    return await mutate_project(request, body)


@router.patch("/projects/{project_id}")
async def edit_project(request: Request, project_id: UUID, body: ProjectPatch):
    return await mutate_project(request, body, project_id, "edit")


@router.post("/projects/{project_id}/archive")
async def archive_project(request: Request, project_id: UUID, body: Version):
    return await mutate_project(request, body, project_id, "archive")


@router.post("/projects/{project_id}/restore")
async def restore_project(request: Request, project_id: UUID, body: Version):
    return await mutate_project(request, body, project_id, "restore")
