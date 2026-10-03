# SignalDesk API

Python 3.12.14; uv 0.12.20. Exact application dependencies and hashes are locked in uv.lock; the isolated Hatchling build backend is pinned separately.

## Local setup

Set environment variables in the process, or use root Compose. The application does not load .env itself. Never commit real credentials.

| Variable | Purpose |
|---|---|
| DATABASE_URL | Plain postgresql:// URI. Runtime uses a separate CRUD-only role; migrator needs DDL. |
| APP_ENV | production by default. Explicit development permits HTTP on loopback only. |
| APP_ORIGIN | Exact browser origin, e.g. http://127.0.0.1:8080; production requires HTTPS. No trailing slash. |
| GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET | Dedicated identity-only OAuth app. No repository scopes. |
| GITHUB_REDIRECT_URI | Exactly APP_ORIGIN plus /api/v1/auth/github/callback. |
| MAX_ACCOUNTS | Global real-account admission cap; default 100. Existing accounts still sign in at capacity. |

```sh
uv sync --frozen
# Use migration-role DATABASE_URL:
uv run --frozen alembic upgrade head
# Then runtime-role DATABASE_URL:
uv run --frozen uvicorn signaldesk.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Disable query-bearing API/proxy access logs: OAuth callbacks contain temporary codes and state. SQL exception strings are never logged or returned by application middleware because they may contain bind parameters.

Migration head: 0002_source_collection. Frozen revisions create identity/project tables and the public-source collection tables independently of future application models. No automatic migration runs at API startup. Destructive downgrade is unsupported; use a verified recovery plan. Runtime needs CRUD on application tables and SELECT on alembic_version, not CREATE/ALTER/schema ownership.

## Implemented behavior

- /health/live checks process liveness. /health/ready checks real PostgreSQL and exact schema head within three seconds: HTTP 200 or503, scope database_schema. This says nothing about collector freshness.
- /api/v1/auth/github/start and callback use state, browser binding and PKCE S256 with a single-use ten-minute attempt. Provider calls happen outside database transactions, follow no redirects, have a 12-second total deadline and64KiB response bound. Missing setup returns503 JSON or a safe sign-in redirect for HTML browsers.
- Session/logout/logout-all use an opaque256-bit cookie token with only SHA-256 digest stored. Seven-day absolute and24-hour idle lifetimes. Login rotates the initiating browser's previous session even across accounts. Other browsers retain their own sessions. Production cookies are Secure, HttpOnly, SameSite=Lax and host-only. CSRF token plus exact Origin required for mutations.
- Account timezone PATCH validates IANA data, including Windows via pinned tzdata.
- Projects support owner-scoped create/list/detail/edit/archive/restore. Three active projects per account. Archive retains the record; restore rechecks capacity. Restore archived projects before editing. No hard-delete route.
- Private operations lock the account row before session/project changes, serializing capacity, logout-all and idempotency. expected_version conflicts include the latest authorized project. Create/archive/restore require UUID Idempotency-Key; PATCH accepts it. Replay rechecks authorization and lifecycle.
- Lists obtain total and page from one SQL statement snapshot, including pages beyond the end, sorted by created time and ID descending. Server-derived top-level account_id lets clients reject results after account switching. active_source_count counts active subscriptions and is zero for archived projects.

Account locking deliberately serializes small-pilot requests per owner. OAuth admission uses a separate PostgreSQL advisory transaction lock. Sign-in starts are bounded across processes at20/minute per peer IP and200/minute globally. Forwarded IP headers are not trusted, so a proxy may make the peer limit shared. Account deletion/backup erasure remains a gate before external pilot users. Expired session/idempotency maintenance is not yet a scheduled worker.

No insecure login bypass, demo identity, inbox or follow-up APIs are exposed. OAuth provider tokens are discarded after identity validation. GitHub authorization revocation does not immediately revoke local sessions; logout-all and bounded expiry are the current controls. Public-source collection uses the separate optional collector credential described below.

## Verification

```sh
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -m "not integration"
# Set TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test:
uv run --frozen pytest -m integration
```

Integration tests fail instead of silently skipping missing PostgreSQL. Identity/project fixtures create a random sdtest_<uuid> schema, apply real Alembic migrations, and drop only that generated schema afterward. No application public table is truncated. The separate readiness test expects the test database public schema already migrated by infrastructure setup.

Tests cover two-owner isolation, concurrent project limits/restores, idempotency, stale edits, archive lifecycle, CSRF/Origin including non-ASCII headers, idle/absolute/revoked sessions, logout-all/write races, OAuth browser binding/expiry/replay/concurrency, PKCE, hashed session storage, rotation and admission limits. HTTP provider fixtures test fixed destinations, extra scopes, redirects, invalid identity, oversized bodies and timeouts. Fixtures are not live GitHub-login evidence; record that smoke check separately.

Sources: [SQLAlchemy metadata](https://pypi.org/project/SQLAlchemy/), [Alembic metadata](https://pypi.org/project/alembic/), [GitHub OAuth](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps), [uv lockfiles](https://docs.astral.sh/uv/concepts/projects/sync/).

## Phase 4: public GitHub source collection

Set optional GITHUB_COLLECTOR_TOKEN for public-read collection, separate from the OAuth identity secret. Never expose this token in frontend configuration. Without it GitHub's smaller anonymous budget applies; provider response throttling remains authoritative. Run the separate worker with `uv run --frozen python -m signaldesk.worker` after migrations. The API only queues durable work; it does not execute collection in the request.

Added APIs under /api/v1:

- GET/POST /projects/{id}/subscriptions, GET/PATCH /subscriptions/{id}, POST pause/resume/remove. Create takes repository plus include_prereleases, returning account_id/subscription/run/reused/restored. Re-adding a removed source retains its row, activation, completed bootstrap selection and history. It does not bypass manual cooldown.
- POST /subscriptions/{id}/checks accepts an empty JSON object and Idempotency-Key. Returns202 for a new queued run or200 for coalesced active work. Cooldown returns429 with Retry-After and error.retry_at.
- GET /subscriptions/{id}/runs and /runs/{run_id}: sanitized shared-public-source run projection; no leases, private subscriber counts or requester identity.
- GET /subscriptions/{id}/releases supports literal text search, prerelease filter and SQL pagination. Detail returns plain source body plus up to50 newest revisions ordered by revision ordinal; revisions_truncated discloses older omitted entries.

All list envelopes and detail/mutation responses include server-derived account_id for browser account-switch fencing. Lists have page/page_size/total/has_next/as_of and compute rows/count from one SQL snapshot. Source history remains accessible to the owner after removal/archive; mutations on archived projects are forbidden until restoration.

Private authorization and CSRF checks run before idempotency replay and before repository validation. Validation makes no network call while private transaction locks are held. It is followed by fresh session/ownership/project-state checks before writes. An owner validation quota of20/minute persists even when the remote provider fails; exact cached retries skip network and quota. Source capacity counts20 distinct active repository IDs across active projects, under the same account lock, including project restoration. Sharing one repository across projects consumes one distinct slot.

Current collection is an explicit manual first30-entry window. Scheduling, automatic retry/backoff, ten-page routine scanning and inbox/decision APIs are later phases. next_check_at is null. Partial/failure warnings remain visible while another run queues; connected requires completed subscription bootstrap and actual full-success evidence for the current bounded check. Captured prereleases remain in history even when excluded from automatic review delivery.

Subscription tests use real disposable PostgreSQL and an injected metadata provider. They verify two-owner and source-attachment isolation, reauthentication after unlocked HTTP, duplicate/replayed concurrent requests,20-source/create-restore races, pause/resume/stale versions, manual coalescing/cooldown, preserved bootstrap on re-add, truthful partial state, literal search/pagination, revision order and provider/throttle failures. They are not evidence of a live GitHub collection; record public-source smoke separately.
