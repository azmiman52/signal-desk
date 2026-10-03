import httpx
import pytest

from signaldesk.config import Settings
from signaldesk.errors import APIError
from signaldesk.main import create_app
from signaldesk.provider import GitHubProvider


def settings():
    return Settings(
        app_env="development",
        app_origin="http://127.0.0.1:8080",
        github_client_id="fixture",
        github_client_secret="fixture-secret",
        github_redirect_uri="http://127.0.0.1:8080/api/v1/auth/github/callback",
    )


async def test_oauth_unconfigured_has_safe_browser_and_json_errors():
    app = create_app(settings=Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://localhost"
    ) as c:
        response = await c.get("/api/v1/auth/github/start")
        assert (
            response.status_code == 503
            and response.json()["error"]["code"] == "oauth_not_configured"
        )
        response = await c.get("/api/v1/auth/github/start", headers={"accept": "text/html"})
        assert (
            response.status_code == 303
            and response.headers["location"] == "/sign-in?error=oauth_not_configured"
        )


async def test_provider_fixed_destinations_and_pkce(monkeypatch):
    real_client = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "github.com":
            assert b"code_verifier=fixture-verifier" in request.content
            return httpx.Response(200, json={"access_token": "fixture-token", "scope": ""})
        return httpx.Response(200, json={"id": 1234, "login": "fixture-user"})

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await GitHubProvider().identity(settings(), "fixture-code", "fixture-verifier") == (
        1234,
        "fixture-user",
    )
    assert [str(c.url) for c in calls] == [
        "https://github.com/login/oauth/access_token",
        "https://api.github.com/user",
    ]
    assert calls[1].headers["authorization"] == "Bearer fixture-token"


@pytest.mark.parametrize("fault", ["redirect", "oversize", "scopes", "identity", "timeout"])
async def test_provider_rejects_untrusted_responses_without_secret_leaks(monkeypatch, fault):
    real_client = httpx.AsyncClient

    def handler(request):
        if fault == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.example"})
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        if fault == "timeout":
            raise httpx.ReadTimeout("fixture-secret", request=request)
        if request.url.host == "github.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-token",
                    "scope": "repo" if fault == "scopes" else "",
                },
            )
        return httpx.Response(200, json={"id": True, "login": "invalid"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(APIError) as caught:
        await GitHubProvider().identity(settings(), "fixture-code", "fixture-verifier")
    assert caught.value.code == "provider_unavailable"
    assert "fixture-secret" not in caught.value.message


def test_settings_fail_closed_for_production_and_redirect():
    with pytest.raises(ValueError):
        Settings(app_origin="http://example.com")
    with pytest.raises(ValueError):
        Settings(app_env="development", app_origin="http://example.com")
    with pytest.raises(ValueError):
        Settings(github_redirect_uri="https://attacker.example/callback")
    assert Settings().cookie_name.startswith("__Host-")
