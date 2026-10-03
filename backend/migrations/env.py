import asyncio
import os

from alembic import context

from signaldesk.db import make_engine, metadata


def migrate(connection):
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = make_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.config.attributes.get("connection") is not None:
    migrate(context.config.attributes["connection"])
else:
    asyncio.run(online())
