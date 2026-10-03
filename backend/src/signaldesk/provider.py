import asyncio
import json

import httpx

from signaldesk.errors import APIError


class GitHubProvider:
    """Identity-only exchange; no token persistence, redirects, or user-supplied URLs."""

    async def identity(self, settings, code: str, verifier: str):
        try:
            async with asyncio.timeout(12):
                async with httpx.AsyncClient(
                    timeout=5, follow_redirects=False, trust_env=False
                ) as c:
                    token = await self._json(
                        c,
                        "POST",
                        "https://github.com/login/oauth/access_token",
                        data={
                            "client_id": settings.github_client_id,
                            "client_secret": settings.github_client_secret,
                            "redirect_uri": settings.github_redirect_uri,
                            "code": code,
                            "code_verifier": verifier,
                        },
                        headers={"Accept": "application/json"},
                    )
                    if not isinstance(token.get("access_token"), str) or not token["access_token"]:
                        raise ValueError("Missing token")
                    # Fail closed if this identity application unexpectedly gains extra scopes.
                    if token.get("scope", "").strip():
                        raise ValueError("Unexpected scopes")
                    user = await self._json(
                        c,
                        "GET",
                        "https://api.github.com/user",
                        headers={
                            "Authorization": "Bearer " + token["access_token"],
                            "Accept": "application/vnd.github+json",
                        },
                    )
                    if type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
                        raise ValueError("Invalid provider identity")
                    name = user.get("name") or user.get("login")
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("Invalid provider name")
                    return user["id"], name.strip()[:200]
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
            raise APIError(
                502, "provider_unavailable", "GitHub sign-in could not be completed."
            ) from exc

    @staticmethod
    async def _json(client, method, url, **kwargs):
        async with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError("Provider response too large")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("Invalid response")
            return value
