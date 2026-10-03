"""Public GitHub release reads with a shared, durable provider gate."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from signaldesk.db import provider_gates

API_ROOT = "https://api.github.com"
GATE_KEY = "github_public"
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
ERROR_MESSAGES = {
    "provider_busy": "GitHub collection is busy. Try again shortly.",
    "rate_limited": "GitHub has delayed requests. Retry after the indicated time.",
    "provider_unavailable": "GitHub could not be checked. Try again later.",
    "source_unavailable": "This public GitHub repository is unavailable.",
    "private_repository": "Only public GitHub repositories are supported.",
    "repository_mismatch": "The repository identity changed. Add the intended repository again.",
    "invalid_repository": "Enter a public github.com repository URL or owner/repository.",
    "invalid_response": "GitHub returned an unsupported or oversized response.",
    "collector_credentials": "GitHub collection credentials need operator attention.",
    "lease_expired": "The collection worker stopped before finishing. Request another check.",
    "worker_failed": "The collection could not finish. Request another check.",
    "coverage_limited": "Checked 30 entries; older releases were not checked.",
    "malformed_entries": "Some release entries could not be imported.",
    "cooldown": "Please wait before checking this source again.",
}


class CollectionError(Exception):
    def __init__(self, code, message=None, status=502, retry_at=None):
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, ERROR_MESSAGES["provider_unavailable"])
        self.status = status
        self.retry_at = retry_at
        super().__init__(self.message)


def repository_name(value):
    if not isinstance(value, str) or len(value) > 300:
        raise CollectionError("invalid_repository", status=422)
    value = value.strip()
    if value.startswith("https://"):
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise CollectionError("invalid_repository", status=422) from exc
        if (
            parsed.netloc.lower() != "github.com"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise CollectionError("invalid_repository", status=422)
        value = parsed.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not REPOSITORY.fullmatch(value) or value.split("/")[1] in {".", ".."}:
        raise CollectionError("invalid_repository", status=422)
    return value


def _retry_time(headers, now):
    times = []
    retry = headers.get("retry-after")
    if retry:
        try:
            times.append(now + timedelta(seconds=max(0, int(retry))))
        except (ValueError, OverflowError):
            try:
                date = parsedate_to_datetime(retry)
                if date.tzinfo:
                    times.append(date)
            except (ValueError, TypeError, OverflowError):
                pass
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            times.append(datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), UTC))
        except (KeyError, ValueError, OverflowError, OSError):
            pass
    return max([now + timedelta(seconds=60), *times]) + timedelta(seconds=1)


@dataclass
class ReleasePage:
    repository: dict
    entries: list
    rejections: list
    discovered: int
    has_more: bool
    selected_ids: list | None = None


class GitHubReleases:
    def __init__(self, engine, token=None, transport=None):
        self.engine = engine
        self._token = token
        self._transport = transport

    async def _acquire(self):
        token = uuid4()
        async with self.engine.begin() as conn:
            await conn.execute(insert(provider_gates).values(key=GATE_KEY).on_conflict_do_nothing())
            row = (
                (
                    await conn.execute(
                        select(provider_gates)
                        .where(provider_gates.c.key == GATE_KEY)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = await conn.scalar(select(func.clock_timestamp()))
            if row["not_before"] and row["not_before"] > now:
                raise CollectionError("rate_limited", status=429, retry_at=row["not_before"])
            if row["lease_expires_at"] and row["lease_expires_at"] > now:
                raise CollectionError("provider_busy", status=429, retry_at=row["lease_expires_at"])
            await conn.execute(
                update(provider_gates)
                .where(provider_gates.c.key == GATE_KEY)
                .values(lease_token=token, lease_expires_at=now + timedelta(seconds=30))
            )
        return token

    async def _release(self, token, retry_at):
        async with self.engine.begin() as conn:
            await conn.execute(
                update(provider_gates)
                .where(provider_gates.c.key == GATE_KEY, provider_gates.c.lease_token == token)
                .values(
                    lease_token=None,
                    lease_expires_at=None,
                    not_before=func.greatest(provider_gates.c.not_before, retry_at)
                    if retry_at
                    else provider_gates.c.not_before,
                )
            )

    async def _request(self, path):
        # Every caller supplies a constructed path; redirects never escape GitHub.
        if not re.fullmatch(
            r"/(?:repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|repositories/[0-9]+)"
            r"(?:/releases\?per_page=30&page=1)?",
            path,
        ):
            raise CollectionError("invalid_repository", status=422)
        token = await self._acquire()
        retry_at = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "SignalDesk",
        }
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        try:
            async with asyncio.timeout(12):
                async with httpx.AsyncClient(
                    timeout=5, follow_redirects=False, trust_env=False, transport=self._transport
                ) as client:
                    async with client.stream("GET", API_ROOT + path, headers=headers) as response:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 2 * 1024 * 1024:
                                raise CollectionError("invalid_response")
                        now = datetime.now(UTC)
                        if response.headers.get("x-ratelimit-remaining") == "0":
                            retry_at = _retry_time(response.headers, now)
                        if response.status_code in {403, 429}:
                            # Classify the standard message without logging provider text.
                            try:
                                error_data = json.loads(body)
                                message = (
                                    error_data.get("message", "")
                                    if isinstance(error_data, dict)
                                    else ""
                                )
                            except (ValueError, UnicodeDecodeError):
                                message = ""
                            throttled = (
                                response.status_code == 429
                                or retry_at is not None
                                or "retry-after" in response.headers
                                or (isinstance(message, str) and "rate limit" in message.lower())
                            )
                            if not throttled:
                                raise CollectionError("collector_credentials")
                            retry_at = _retry_time(response.headers, now)
                            raise CollectionError("rate_limited", status=429, retry_at=retry_at)
                        if response.status_code == 401:
                            raise CollectionError("collector_credentials")
                        if response.status_code == 404:
                            raise CollectionError("source_unavailable", status=422)
                        if response.status_code in {301, 302, 307, 308}:
                            location = response.headers.get("location", "")
                            if re.fullmatch(
                                r"https://api\.github\.com/repositories/[0-9]+", location
                            ):
                                return None, {"canonical_path": location[len(API_ROOT) :]}
                            raise CollectionError("source_unavailable", status=422)
                        if response.status_code != 200:
                            raise CollectionError("provider_unavailable")
                        try:
                            data = json.loads(body)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise CollectionError("invalid_response") from exc
                        return data, dict(response.headers)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise CollectionError("provider_unavailable") from exc
        finally:
            await self._release(token, retry_at)

    async def validate_repository(self, value):
        name = repository_name(value)
        data, headers = await self._request("/repos/" + name)
        if data is None and "canonical_path" in headers:
            data, _ = await self._request(headers["canonical_path"])
        if not isinstance(data, dict):
            raise CollectionError("invalid_response")
        if data.get("private") is not False or data.get("visibility", "public") != "public":
            raise CollectionError("private_repository", status=422)
        github_id = data.get("id")
        if type(github_id) is not int or not 0 < github_id < 2**63:
            raise CollectionError("invalid_response")
        try:
            canonical = repository_name(data["full_name"])
        except (KeyError, CollectionError) as exc:
            raise CollectionError("invalid_response") from exc
        return {
            "github_id": github_id,
            "repository": canonical,
            "url": "https://github.com/" + canonical,
        }

    async def fetch_releases(self, repository, expected_github_id):
        metadata = await self.validate_repository(repository)
        if metadata["github_id"] != expected_github_id:
            raise CollectionError("repository_mismatch")
        data, headers = await self._request(
            "/repos/" + metadata["repository"] + "/releases?per_page=30&page=1"
        )
        if not isinstance(data, list) or len(data) > 30:
            raise CollectionError("invalid_response")
        entries, rejected = [], []
        seen = set()
        for position, entry in enumerate(data):
            try:
                value = normalize_release(entry)
                if value["github_id"] in seen:
                    raise ValueError("duplicate")
                seen.add(value["github_id"])
                entries.append(value)
            except (ValueError, TypeError, KeyError, OverflowError):
                rejected.append(
                    {
                        "position": position,
                        "reason": "invalid_release",
                        "payload_hash": hashlib.sha256(
                            json.dumps(entry, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                )
        has_more = bool(re.search(r'rel="?next\b', headers.get("link", "")))
        selected_ids = [
            entry["id"]
            for entry in data
            if isinstance(entry, dict) and type(entry.get("id")) is int and 0 < entry["id"] < 2**63
        ]
        return ReleasePage(metadata, entries, rejected, len(data), has_more, selected_ids)


def normalize_release(entry):
    if not isinstance(entry, dict) or entry.get("draft") is not False:
        raise ValueError("not published")
    identifier = entry["id"]
    if type(identifier) is not int or not 0 < identifier < 2**63:
        raise ValueError("id")
    tag = entry["tag_name"]
    if not isinstance(tag, str) or not tag or len(tag) > 1000:
        raise ValueError("tag")
    name, body = entry.get("name"), entry.get("body")
    if name is not None and not isinstance(name, str):
        raise ValueError("title")
    if body is not None and not isinstance(body, str):
        raise ValueError("body")
    title, body = name or tag, body if body is not None else ""
    if not isinstance(title, str) or len(title) > 2000:
        raise ValueError("title")
    if not isinstance(body, str) or len(body.encode()) > 128 * 1024:
        raise ValueError("body")
    if type(entry["prerelease"]) is not bool:
        raise ValueError("prerelease")
    url = entry["html_url"]
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("url")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ValueError("url")
    if not isinstance(entry.get("published_at"), str):
        raise ValueError("publication time")
    for field in (title, tag, body, url):
        if "\x00" in field:
            raise ValueError("unsupported null character")
        field.encode("utf-8")
    published = datetime.fromisoformat(entry["published_at"].replace("Z", "+00:00"))
    if published.tzinfo is None:
        raise ValueError("publication time")
    value = {
        "title": title,
        "tag_name": tag,
        "body": body,
        "url": url,
        "published_at": published.astimezone(UTC).isoformat(),
        "prerelease": entry["prerelease"],
    }
    content_hash = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return {
        **value,
        "published_at": published.astimezone(UTC),
        "github_id": identifier,
        "content_hash": content_hash,
    }
