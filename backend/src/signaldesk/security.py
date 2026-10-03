import hashlib
import secrets
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from signaldesk.db import accounts, rate_limits, sessions
from signaldesk.errors import APIError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def authenticate(request: Request, conn, *, write=False):
    settings = request.app.state.settings
    token = request.cookies.get(settings.cookie_name, "")
    if not token or len(token) > 128:
        raise APIError(401, "unauthenticated", "Sign in to continue.")
    found = await conn.execute(select(sessions).where(sessions.c.token_hash == digest(token)))
    session = found.mappings().first()
    if not session:
        raise APIError(401, "unauthenticated", "Sign in to continue.")
    query = select(accounts).where(accounts.c.id == session["account_id"])
    # All private operations take this same lock before touching session/project
    # rows, making logout-all, capacity checks and retries serialize safely.
    account = (await conn.execute(query.with_for_update())).mappings().first()
    session = (
        (
            await conn.execute(
                select(sessions).where(sessions.c.id == session["id"]).with_for_update()
            )
        )
        .mappings()
        .first()
    )
    now = await conn.scalar(select(func.clock_timestamp()))
    if (
        not account
        or not session
        or session["revoked_at"]
        or session["expires_at"] <= now
        or session["last_seen_at"] <= now - timedelta(hours=24)
    ):
        raise APIError(401, "session_expired", "Your session ended. Sign in again.")
    if write:
        csrf = request.headers.get("x-csrf-token", "")
        if request.headers.get("origin") != settings.app_origin or not secrets.compare_digest(
            csrf.encode(), session["csrf_token"].encode()
        ):
            raise APIError(403, "csrf_failed", "Reload the page before trying again.")
    await conn.execute(
        update(sessions).where(sessions.c.id == session["id"]).values(last_seen_at=now)
    )
    return account, session, now


async def limit(conn, key: str, maximum: int):
    now = await conn.scalar(select(func.clock_timestamp()))
    await conn.execute(
        insert(rate_limits).values(key=key, window_start=now, count=0).on_conflict_do_nothing()
    )
    row = (
        (await conn.execute(select(rate_limits).where(rate_limits.c.key == key).with_for_update()))
        .mappings()
        .one()
    )
    count = 0 if row["window_start"] <= now - timedelta(minutes=1) else row["count"]
    if count >= maximum:
        raise APIError(
            429,
            "rate_limited",
            "Too many sign-in attempts. Try again shortly.",
            headers={"Retry-After": "60"},
        )
    await conn.execute(
        update(rate_limits)
        .where(rate_limits.c.key == key)
        .values(count=count + 1, window_start=now if count == 0 else row["window_start"])
    )


def account_json(account):
    return {
        "id": str(account["id"]),
        "display_name": account["display_name"],
        "timezone": account["timezone"],
        "kind": "real",
    }
