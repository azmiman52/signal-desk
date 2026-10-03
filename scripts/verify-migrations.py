"""Verify fresh/repeated migrations and runtime permissions on a disposable DB."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from alembic import command as alembic_command
from alembic.config import Config
from signaldesk.db import SCHEMA_HEAD, make_engine
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
from migrate import role_url  # noqa: E402


async def check_previous_revision(bootstrap: str) -> None:
    """Upgrade populated Phase 3 in an isolated schema, never downgrade real data."""
    schema = "sdupgrade_" + uuid4().hex
    admin = await asyncpg.connect(bootstrap)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    engine = make_engine(bootstrap, connect_args={"server_settings": {"search_path": schema}})
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    owner_id, project_id, session_id = uuid4(), uuid4(), uuid4()
    try:
        async with engine.begin() as connection:

            def upgrade(sync_connection, revision):
                config.attributes["connection"] = sync_connection
                alembic_command.upgrade(config, revision)

            await connection.run_sync(upgrade, "0001_identity_projects")
            await connection.execute(
                text(
                    "INSERT INTO accounts (id, github_user_id, display_name) "
                    "VALUES (:id, 42, 'Upgrade owner')"
                ),
                {"id": owner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO projects (id, account_id, name, description, version) "
                    "VALUES (:id, :owner, 'Retained project', 'Retained description', 3)"
                ),
                {"id": project_id, "owner": owner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO sessions (id, account_id, token_hash, csrf_token, "
                    "created_at, last_seen_at, expires_at) "
                    "VALUES (:id, :owner, :hash, :csrf, now(), now(), now() + interval '1 day')"
                ),
                {
                    "id": session_id,
                    "owner": owner_id,
                    "hash": "a" * 64,
                    "csrf": "b" * 64,
                },
            )
            await connection.run_sync(upgrade, "head")
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == SCHEMA_HEAD
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM accounts WHERE id=:id"), {"id": owner_id}
                )
                == 1
            )
            project = (
                await connection.execute(
                    text("SELECT name, description, version FROM projects WHERE id=:id"),
                    {"id": project_id},
                )
            ).one()
            assert tuple(project) == ("Retained project", "Retained description", 3)
            assert (
                await connection.scalar(
                    text("SELECT token_hash FROM sessions WHERE id=:id"),
                    {"id": session_id},
                )
                == "a" * 64
            )
            await connection.run_sync(upgrade, "head")
    finally:
        await engine.dispose()
        # Only the random schema created by this invocation is removed.
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
    print("Populated Phase 3 account, session and project upgrade preserved data.")


async def check(stage: str) -> None:
    bootstrap = os.environ["BOOTSTRAP_DATABASE_URL"]
    owner = await asyncpg.connect(bootstrap)
    app = await asyncpg.connect(
        role_url(bootstrap, "signaldesk_app", os.environ["APP_DATABASE_PASSWORD"])
    )
    try:
        if stage == "create":
            # This sentinel belongs only to the dedicated test database.
            await owner.execute("CREATE TABLE public.infra_upgrade_probe (value text NOT NULL)")
            await owner.execute("INSERT INTO public.infra_upgrade_probe VALUES ('preserved')")
        else:
            assert (
                await owner.fetchval("SELECT value FROM public.infra_upgrade_probe") == "preserved"
            )
            assert await app.fetchval("SELECT version_num FROM public.alembic_version")
            assert not await app.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            )
            for statement in (
                "CREATE TABLE public.forbidden_runtime_table (id int)",
                "UPDATE public.alembic_version SET version_num=version_num",
            ):
                try:
                    await app.execute(statement)
                except asyncpg.InsufficientPrivilegeError:
                    pass
                else:
                    raise AssertionError("Runtime unexpectedly allowed schema mutation")
            await owner.execute("DROP TABLE public.infra_upgrade_probe")
    finally:
        await app.close()
        await owner.close()


def main() -> None:
    bootstrap = os.environ["BOOTSTRAP_DATABASE_URL"]
    database = urlsplit(bootstrap).path.removeprefix("/")
    if database != "signaldesk_test":
        raise ValueError("Migration smoke only permits disposable database signaldesk_test")
    asyncio.run(check_previous_revision(bootstrap))
    command = [sys.executable, str(ROOT / "infra" / "migrate.py")]
    subprocess.run(command, cwd=ROOT / "backend", check=True)
    asyncio.run(check("create"))
    subprocess.run(command, cwd=ROOT / "backend", check=True)
    asyncio.run(check("verify"))
    print("Fresh migration, repeated upgrade, retained data and runtime privilege checks passed.")


if __name__ == "__main__":
    main()
