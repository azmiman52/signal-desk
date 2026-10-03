# SignalDesk

SignalDesk helps developers review public software releases in the context of their projects and record maintenance decisions.

This branch adds GitHub sign-in, server-side sessions, private projects, and explicit PostgreSQL migrations to the React/FastAPI foundation. GitHub release collection, review persistence, and a durable worker are still later work. The local stack is not a production deployment.

## Run the local stack

Prerequisite: a running Docker engine with Linux containers and Docker Compose. From the repository root:

```powershell
Copy-Item .env.example .env
docker compose up --build -d --wait
```

Copy the example only if `.env` does not already exist. Open [SignalDesk](http://127.0.0.1:8080). The frontend proxies health/API requests to the backend on the same origin. Direct API diagnostics are available at [liveness](http://127.0.0.1:8000/health/live) and [readiness](http://127.0.0.1:8000/health/ready). Readiness checks database connectivity and the expected Alembic schema revision. It does not establish collection freshness.

All published ports bind to `127.0.0.1`: frontend 8080, API 8000, PostgreSQL 5433. Change `WEB_PORT`, `API_PORT`, or `POSTGRES_PORT` in `.env` if needed. Native frontend development uses its Vite port separately. The example database password is intentionally local-only. Avoid URL-reserved password characters unless the connection URL is encoded.

```powershell
docker compose logs --tail 100 api frontend db
docker compose stop
docker compose up -d --wait
```

`docker compose down` removes this project's containers/network but preserves the PostgreSQL named volume. Do not add `--volumes` to normal shutdown: it deletes local database data. Changing `POSTGRES_PASSWORD` after database initialization does not automatically rotate the persisted PostgreSQL password.

## Native development

### Local GitHub OAuth setup

Create a GitHub **OAuth App** in your account's Developer settings. Set Homepage URL to `http://127.0.0.1:8080` and Authorization callback URL to exactly `http://127.0.0.1:8080/api/v1/auth/github/callback`. Put its client ID and generated client secret in `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in the ignored root `.env`. Do not paste them into issues, commit them, or put them in frontend variables. Missing credentials produce an explicit sign-in configuration error; no local auth bypass is enabled.

Keep `APP_ORIGIN` and `GITHUB_REDIRECT_URI` consistent with the origin you open. `localhost` and `127.0.0.1` are different origins. Changing the web port requires updating both values and the registered callback. Compose is explicitly development-only; production backend configuration must use HTTPS and secure cookies. Restart/recreate the API after changing credentials with `docker compose up -d --force-recreate api`; do not print resolved Compose environment output, which can reveal secrets.

For native Vite development, configure its actual origin and matching GitHub callback separately and forward `/api` to port 8000. The easiest OAuth verification path is the same-origin Compose URL above. Native shells must explicitly provide `APP_ENV`, `APP_ORIGIN`, `GITHUB_REDIRECT_URI`, and credentials through their environment; the root `.env` is not automatically loaded by native commands.

### Migration and database roles

The `migrate` service completes before API startup. It uses the existing `signaldesk` bootstrap administrator to create `signaldesk_migrator` and `signaldesk_app`, then runs `python -m alembic upgrade head` as the migrator. The migrator owns the application schema and can perform DDL; neither new role has superuser, role-management, or database-creation privileges. The API receives only the runtime URL: table CRUD and read-only schema-version access, without schema CREATE or migration-version writes.

Role provisioning is scoped to this application's database. It changes public-schema ownership/permissions but does not drop tables or volumes. It must not run against an unrelated shared database. Runtime/migrator passwords come from `.env` with explicit local-only defaults; provisioning reapplies them on each migration run. The bootstrap password for an existing volume must match its previously initialized value. A migration failure blocks API startup.

To upgrade the local running stack, stop API traffic first, run the explicit migration, then recreate the matching API/frontend:

```powershell
docker compose stop api frontend
docker compose run --build --rm migrate
docker compose up --build -d --wait
```

Do not continue the last command if migration fails. Retain a backup before later data-bearing schema changes. The initial revision has no prior application schema to migrate; CI exercises fresh initialization and an idempotent repeat upgrade preserving a sentinel row. Future revisions must add a populated previous-revision upgrade test. No automatic downgrade or destructive reset is provided.

Uvicorn access logging is disabled. nginx access logs contain path-only URIs without query strings, and nginx error logging is suppressed locally because upstream errors can contain OAuth codes. Keep callback codes, state, cookies, and credentials out of diagnostic output.

### Start native processes

Use Python 3.12.14, uv 0.12.20, and Node 22.23.3 for parity with containers/CI. Install dependencies from committed locks rather than resolving new versions during ordinary setup.

Initialize or upgrade the local schema with `docker compose run --build --rm migrate`. This starts the database dependency, creates restricted roles, and migrates without deleting the existing volume. In one terminal:

```powershell
Set-Location backend
$env:DATABASE_URL = 'postgresql://signaldesk_app:signaldesk-app-local-only@127.0.0.1:5433/signaldesk'
uv sync --frozen
uv run --frozen uvicorn signaldesk.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

Use the actual local password if changed. `.env` is consumed by Compose; native commands set their own environment explicitly. In another terminal:

```powershell
Set-Location frontend
npm ci
npm run dev -- --host 127.0.0.1
```

The frontend dev server proxies health requests to port 8000. Native Unix shells use `export DATABASE_URL=...` in place of PowerShell environment syntax.

## Verify changes

Backend unit checks, from `backend`:

```powershell
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -p no:cacheprovider -m 'not integration'
```

Frontend checks, from `frontend`:

```powershell
npm ci
npm run lint
npm run typecheck
npm test
npm run build
```

Run PostgreSQL integration checks from the repository root:

```powershell
./scripts/test-postgres.ps1
```

The script starts a uniquely named disposable Compose project, uses a test database on loopback port 5434 backed by temporary memory, verifies fresh and repeat migrations with retained data and runtime permissions, runs backend tests with the pytest cache disabled, and removes only that invocation's containers. It never deletes the normal local database volume. Port 5434 must be free; run one invocation at a time. Pass `-Uv` with a path if uv is not on PATH. A direct integration invocation requires `TEST_DATABASE_URL` pointing at a dedicated test database and `uv run --frozen pytest -p no:cacheprovider -m integration`; missing or unreachable PostgreSQL is a failure, not a successful skipped check.

CI runs locked installs, backend lint/unit/real-PostgreSQL integration tests and migration/privilege smoke checks, frontend lint/type/tests/build, and an isolated full Compose build with frontend/readiness smoke checks. The workflow runs on pushes and pull requests with read-only repository permissions. A workflow file is not evidence that the hosted checks passed; inspect the actual run for the current commit.

## Repository layout and configuration

- `backend/`: application, health checks, tests, Python lockfile.
- `frontend/`: browser app, frontend tests, npm lockfile.
- `infra/`: API/frontend Dockerfiles, nginx routing, disposable database topology.
- `scripts/`: scoped developer verification helpers.
- `.github/workflows/ci.yml`: hosted verification.

Environment files, credentials, caches, generated output, local artifacts, and internal planning files are excluded from version control/build context where appropriate. Keep technical onboarding here. Do not put credentials in frontend build variables. Local runtime and migration roles are separate. OAuth credentials stay in ignored local configuration. Worker, backups/restore drills, public TLS, and production secret management remain later work before hosting real users.

## Runtime provenance and current limits

Container/runtime patches were selected on 30 September 2026 against official release pages: [Python 3.12.14](https://www.python.org/downloads/source/), [PostgreSQL 17.11](https://www.postgresql.org/support/versioning/), [Node 22.23.3](https://nodejs.org/download/release/latest-v22.x/SHASUMS256.txt), and [nginx 1.30.5](https://nginx.org/en/download.html). uv installation follows its [Docker integration guidance](https://docs.astral.sh/uv/guides/integration/docker/), pinned to 0.12.20. Exact patch tags reduce drift; they are not immutable image digests. Registry availability, image execution, and hosted CI must be verified separately from release-documentation checks.

If Docker reports a missing named pipe on Windows, start Docker Desktop and wait for its Linux engine. A Docker CLI version alone does not prove the daemon is available. If readiness returns 503, inspect database health/connection configuration; liveness 200 alone is not database readiness. No reset or volume deletion is needed to diagnose either condition.
