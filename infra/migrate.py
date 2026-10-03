"""Local database role provisioning and explicit migration, never API startup work.

The bootstrap credential belongs only to this one-shot service. This script is
intended for SignalDesk's own local/disposable database, not a shared database.
"""

import asyncio
import os
import subprocess
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg

MIGRATOR = "signaldesk_migrator"
RUNTIME = "signaldesk_app"


def role_url(bootstrap: str, role: str, password: str) -> str:
    parts = urlsplit(bootstrap)
    if parts.scheme not in {"postgresql", "postgres"} or not parts.hostname:
        raise ValueError("A plain PostgreSQL bootstrap URL is required")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = f"{role}:{quote(password, safe='')}@{host}:{parts.port or 5432}"
    return urlunsplit((parts.scheme, authority, parts.path, parts.query, ""))


async def provision(bootstrap: str) -> None:
    connection = await asyncpg.connect(bootstrap)
    try:
        async with connection.transaction():
            for role, variable in (
                (MIGRATOR, "MIGRATOR_PASSWORD"),
                (RUNTIME, "APP_DATABASE_PASSWORD"),
            ):
                password = os.environ[variable]
                if not password:
                    raise ValueError(f"{variable} must not be empty")
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", role
                )
                if not exists:
                    await connection.execute(f'CREATE ROLE "{role}" LOGIN')
                # Identifiers are fixed constants, values quoted by PostgreSQL.
                literal = await connection.fetchval("SELECT quote_literal($1::text)", password)
                await connection.execute(
                    f'ALTER ROLE "{role}" WITH LOGIN NOSUPERUSER NOCREATEDB '
                    f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {literal}"
                )
            database = await connection.fetchval("SELECT current_database()")
            identifier = await connection.fetchval("SELECT quote_ident($1::text)", database)
            await connection.execute(f'GRANT CONNECT ON DATABASE {identifier} TO "{RUNTIME}"')
            await connection.execute(
                f'GRANT CONNECT, CREATE ON DATABASE {identifier} TO "{MIGRATOR}"'
            )
            await connection.execute(f'ALTER SCHEMA public OWNER TO "{MIGRATOR}"')
            await connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            await connection.execute(f'GRANT USAGE ON SCHEMA public TO "{RUNTIME}"')
            await connection.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{MIGRATOR}" IN SCHEMA public '
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{RUNTIME}"'
            )
    finally:
        await connection.close()


async def grant_runtime(bootstrap: str) -> None:
    connection = await asyncpg.connect(bootstrap)
    try:
        async with connection.transaction():
            await connection.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f'TO "{RUNTIME}"'
            )
            await connection.execute(
                f'REVOKE INSERT, UPDATE, DELETE ON public.alembic_version FROM "{RUNTIME}"'
            )
    finally:
        await connection.close()


def main() -> None:
    bootstrap = os.environ["BOOTSTRAP_DATABASE_URL"]
    asyncio.run(provision(bootstrap))
    environment = os.environ.copy()
    environment["DATABASE_URL"] = role_url(bootstrap, MIGRATOR, os.environ["MIGRATOR_PASSWORD"])
    # Do not give the migration subprocess the bootstrap credential.
    environment.pop("BOOTSTRAP_DATABASE_URL", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=environment,
        check=True,
        capture_output=True,
    )
    asyncio.run(grant_runtime(bootstrap))
    print("Schema migrated; runtime role grants applied.")


if __name__ == "__main__":
    try:
        main()
    except (
        KeyError,
        ValueError,
        OSError,
        asyncpg.PostgresError,
        subprocess.CalledProcessError,
    ) as error:
        # Driver errors can contain SQL/passwords; never print the raw exception.
        print(
            f"Migration setup failed ({type(error).__name__}); API startup blocked.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
