"""Run independently from the API: python -m signaldesk.worker."""

import asyncio
import logging
import os
import signal

from signaldesk.collection import CollectionWorker
from signaldesk.db import make_engine
from signaldesk.github_releases import GitHubReleases


async def serve():
    engine = make_engine(os.environ["DATABASE_URL"])
    worker = CollectionWorker(engine, GitHubReleases(engine, os.getenv("GITHUB_COLLECTOR_TOKEN")))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            # Windows asyncio.run handles Ctrl-C cancellation; leases recover forced exits.
            pass
    try:
        while not stop.is_set():
            try:
                worked = await worker.run_once()
                delay = 1 if worked else 2
            except Exception:
                # Exception text may contain connection details. Keep operational logs generic.
                logging.getLogger(__name__).warning(
                    "Collection storage unavailable; retrying later"
                )
                delay = 5
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
