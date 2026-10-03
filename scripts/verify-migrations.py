"""Verify fresh/repeated migrations and runtime permissions on a disposable DB."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
from migrate import role_url  # noqa: E402


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
    command = [sys.executable, str(ROOT / "infra" / "migrate.py")]
    subprocess.run(command, cwd=ROOT / "backend", check=True)
    asyncio.run(check("create"))
    subprocess.run(command, cwd=ROOT / "backend", check=True)
    asyncio.run(check("verify"))
    print("Fresh migration, repeated upgrade, retained data and runtime privilege checks passed.")


if __name__ == "__main__":
    main()
