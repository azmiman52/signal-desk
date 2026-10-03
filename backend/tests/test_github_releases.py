import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from signaldesk.db import provider_gates
from signaldesk.github_releases import (
    GATE_KEY,
    CollectionError,
    GitHubReleases,
    normalize_release,
    repository_name,
)


def release(**changes):
    return {
        "id": 10,
        "draft": False,
        "prerelease": False,
        "tag_name": "v1",
        "name": None,
        "body": None,
        "html_url": "https://github.com/a/b/releases/tag/v1",
        "published_at": "2026-10-01T01:00:00Z",
        **changes,
    }


@pytest.mark.parametrize(
    "value",
    [
        "https://[bad",
        "http://github.com/a/b",
        "https://evil.test/a/b",
        "https://github.com@evil.test/a/b",
        "https://github.com/a/b?token=x",
        "a/../b",
        "https://github.com/a/b/tree/main",
        "a/b%2Ffoo",
        "a/b\\foo",
        "https://github.com:443/a/b",
    ],
)
def test_repository_rejects_unsafe_input(value):
    with pytest.raises(CollectionError):
        repository_name(value)


def test_repository_accepts_supported_forms():
    assert repository_name("https://github.com/Example/repo.git/") == "Example/repo"
    assert repository_name("Example/repo") == "Example/repo"


@pytest.mark.parametrize(
    "changes",
    [
        {"published_at": None},
        {"published_at": 5},
        {"published_at": "2026-10-01"},
        {"id": True},
        {"draft": True},
        {"body": False},
        {"name": 0},
        {"prerelease": 0},
        {"html_url": "https://evil.test/a"},
    ],
)
def test_normalize_invalid_entries(changes):
    with pytest.raises((ValueError, TypeError, KeyError)):
        normalize_release(release(**changes))


@pytest.mark.integration
async def test_public_request_headers_and_bounds(database):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/releases"):
            assert dict(request.url.params) == {"per_page": "30", "page": "1"}
            return httpx.Response(
                200,
                json=[release(), release(id=11, published_at=None)],
                headers={"link": '<https://api.github.com/repos/a/b/releases?page=2>; rel="next"'},
            )
        return httpx.Response(200, json={"id": 5, "full_name": "a/b", "private": False})

    provider = GitHubReleases(
        database, token="collector-only", transport=httpx.MockTransport(handle)
    )
    page = await provider.fetch_releases("a/b", 5)
    assert page.has_more and page.discovered == 2 and len(page.entries) == len(page.rejections) == 1
    assert page.selected_ids == [10, 11]
    assert len(requests) == 2
    assert all(r.url.host == "api.github.com" for r in requests)
    assert all(r.headers["Authorization"] == "Bearer collector-only" for r in requests)
    assert all(r.headers["X-GitHub-Api-Version"] == "2026-03-10" for r in requests)


@pytest.mark.integration
async def test_rate_limit_gate_shared_across_instances(database):
    calls = []
    reset = datetime.now(UTC) + timedelta(hours=1)

    def handle(request):
        calls.append(request)
        return httpx.Response(
            429,
            json={"message": "sensitive-provider-content"},
            headers={
                "retry-after": "120",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(reset.timestamp())),
            },
        )

    p1 = GitHubReleases(database, transport=httpx.MockTransport(handle))
    p2 = GitHubReleases(database, transport=httpx.MockTransport(handle))
    for p in [p1, p2]:
        with pytest.raises(CollectionError) as exc:
            await p.validate_repository("a/b")
        assert exc.value.code == "rate_limited" and exc.value.retry_at >= reset
        assert "sensitive" not in str(exc.value)
    assert len(calls) == 1
    async with database.connect() as conn:
        row = (await conn.execute(select(provider_gates))).mappings().one()
        assert row["not_before"] and row["lease_token"] is None


@pytest.mark.integration
async def test_concurrent_gate_busy_and_released_after_timeout(database):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def handle(request):
        entered.set()
        await finish.wait()
        raise httpx.ReadTimeout("secret URL", request=request)

    provider = GitHubReleases(database, transport=httpx.MockTransport(handle))
    task = asyncio.create_task(provider.validate_repository("a/b"))
    await entered.wait()
    try:
        with pytest.raises(CollectionError) as err:
            await provider.validate_repository("a/b")
        assert err.value.code == "provider_busy"
    finally:
        finish.set()
    with pytest.raises(CollectionError) as err:
        await task
    assert err.value.code == "provider_unavailable" and "secret" not in str(err.value)
    async with database.connect() as conn:
        assert (await conn.execute(select(provider_gates))).mappings().one()["lease_token"] is None


@pytest.mark.integration
async def test_gate_fences_expired_owner_release(database):
    provider = GitHubReleases(database)
    first = await provider._acquire()
    async with database.begin() as conn:
        await conn.execute(
            update(provider_gates)
            .where(provider_gates.c.key == GATE_KEY)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = await provider._acquire()
    await provider._release(first, None)
    async with database.connect() as conn:
        assert (await conn.execute(select(provider_gates))).mappings().one()[
            "lease_token"
        ] == second
    await provider._release(second, None)


@pytest.mark.integration
@pytest.mark.parametrize(
    "response,code",
    [
        (
            httpx.Response(200, json={"id": 1, "full_name": "a/b", "private": True}),
            "private_repository",
        ),
        (httpx.Response(401, text="secret"), "collector_credentials"),
        (httpx.Response(404, text="secret"), "source_unavailable"),
        (httpx.Response(200, content=b"not json"), "invalid_response"),
        (httpx.Response(302, headers={"location": "https://evil.test"}), "source_unavailable"),
        (httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)), "invalid_response"),
    ],
)
async def test_provider_failure_is_safe(database, response, code):
    calls = []

    def handle(request):
        calls.append(request)
        return response

    provider = GitHubReleases(database, transport=httpx.MockTransport(handle))
    with pytest.raises(CollectionError) as exc:
        await provider.validate_repository("a/b")
    assert exc.value.code == code and "secret" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.integration
async def test_rename_and_identity_mismatch(database):
    def handle(request):
        if request.url.path == "/repos/a/b":
            return httpx.Response(
                301, headers={"location": "https://api.github.com/repositories/5"}
            )
        return httpx.Response(200, json={"id": 5, "full_name": "renamed/repo", "private": False})

    provider = GitHubReleases(database, transport=httpx.MockTransport(handle))
    assert (await provider.validate_repository("a/b"))["repository"] == "renamed/repo"
    with pytest.raises(CollectionError) as exc:
        await provider.fetch_releases("a/b", 6)
    assert exc.value.code == "repository_mismatch"


@pytest.mark.integration
@pytest.mark.parametrize(
    "status,headers,message,code",
    [
        (403, {}, "Forbidden", "collector_credentials"),
        (403, {}, "You have exceeded a secondary rate limit.", "rate_limited"),
        (429, {}, "Throttled", "rate_limited"),
    ],
)
async def test_forbidden_vs_secondary_limit(database, status, headers, message, code):
    provider = GitHubReleases(
        database,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, headers=headers, json={"message": message})
        ),
    )
    before = datetime.now(UTC)
    with pytest.raises(CollectionError) as exc:
        await provider.validate_repository("a/b")
    assert exc.value.code == code
    if code == "rate_limited":
        assert exc.value.retry_at >= before + timedelta(seconds=60)


@pytest.mark.integration
async def test_rejects_oversized_list_without_silent_truncation(database):
    def handle(request):
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=[release(id=i) for i in range(1, 32)])
        return httpx.Response(200, json={"id": 1, "full_name": "a/b", "private": False})

    provider = GitHubReleases(database, transport=httpx.MockTransport(handle))
    with pytest.raises(CollectionError) as exc:
        await provider.fetch_releases("a/b", 1)
    assert exc.value.code == "invalid_response"


@pytest.mark.integration
async def test_malformed_timestamp_and_null_entry_is_quarantined(database):
    def handle(request):
        if request.url.path.endswith("/releases"):
            return httpx.Response(
                200,
                json=[
                    release(),
                    release(id=11, published_at="0001-01-01T00:00:00+23:59"),
                    release(id=12, body="bad\x00body"),
                ],
            )
        return httpx.Response(200, json={"id": 1, "full_name": "a/b", "private": False})

    page = await GitHubReleases(database, transport=httpx.MockTransport(handle)).fetch_releases(
        "a/b", 1
    )
    assert len(page.entries) == 1 and len(page.rejections) == 2
