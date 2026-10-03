"""Shared project/subscription capacity rules; caller holds the owner's account lock."""

from sqlalchemy import distinct, func, or_, select

from signaldesk.db import projects, subscriptions
from signaldesk.errors import APIError


async def ensure_source_capacity(conn, owner, *, candidate_source=None, restore_project=None):
    project_scope = projects.c.archived_at.is_(None)
    if restore_project is not None:
        project_scope = or_(project_scope, projects.c.id == restore_project)
    active = (
        select(subscriptions.c.source_id)
        .select_from(subscriptions.join(projects, subscriptions.c.project_id == projects.c.id))
        .where(projects.c.account_id == owner, project_scope, subscriptions.c.state == "active")
    )
    current = active.subquery()
    count = await conn.scalar(select(func.count(distinct(current.c.source_id))))
    if candidate_source is not None:
        present = await conn.scalar(
            select(current.c.source_id).where(current.c.source_id == candidate_source).limit(1)
        )
        count += 0 if present else 1
    if count > 20:
        raise APIError(
            409,
            "capacity_exceeded",
            "Limit: 20 distinct active repositories. Pause a source first.",
        )


def active_source_count(project_id):
    return (
        select(func.count())
        .select_from(subscriptions)
        .where(
            subscriptions.c.project_id == project_id,
            subscriptions.c.state == "active",
        )
        .scalar_subquery()
    )
