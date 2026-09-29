# SignalDesk API foundation

Python 3.12.14; uv 0.12.20. From this directory:

```sh
uv sync --frozen
uv run uvicorn signaldesk.main:app --host 127.0.0.1 --port 8000
```

Set `DATABASE_URL` to a PostgreSQL connection URI in the process environment. No configuration is required for liveness; missing or unavailable PostgreSQL correctly returns HTTP 503 on readiness. Never commit real credentials. Root Compose/environment examples provide local configuration.

- `GET /health/live`: process is serving requests; no database access.
- `GET /health/ready`: bounded real `SELECT 1`, three-second overall deadline, HTTP 200 or 503. No credentials or provider errors in response. This checks database connectivity only; migrations and collector freshness are future work.

Verification:

```sh
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -m "not integration"
# Set TEST_DATABASE_URL to a dedicated PostgreSQL database first:
uv run --frozen pytest -m integration
```

Integration selection fails when the database is absent/unreachable. It executes a read-only query and creates no schema. Direct dependencies are exact in pyproject.toml and transitive versions/hashes are recorded in uv.lock. The Hatchling build backend is pinned separately because isolated build requirements are outside the runtime lock.

Sources used for compatibility: [FastAPI package metadata](https://pypi.org/project/fastapi/), [asyncpg metadata](https://pypi.org/project/asyncpg/), [Python releases](https://www.python.org/downloads/), [uv lockfiles](https://docs.astral.sh/uv/concepts/projects/sync/), [Hatchling history](https://hatch.pypa.io/dev/history/hatchling/).
