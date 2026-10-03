import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default="", repr=False)
    app_env: str = "production"
    app_origin: str = "https://localhost"
    github_client_id: str = ""
    github_client_secret: str = field(default="", repr=False)
    github_redirect_uri: str = ""
    max_accounts: int = 100

    def __post_init__(self):
        origin = urlsplit(self.app_origin)
        if self.app_env not in {"development", "production"}:
            raise ValueError("APP_ENV must be development or production")
        if origin.path or origin.query or origin.fragment or origin.username or not origin.netloc:
            raise ValueError("APP_ORIGIN must be an exact origin without a path")
        if self.app_env == "production" and origin.scheme != "https":
            raise ValueError("Production requires an HTTPS APP_ORIGIN")
        if self.app_env == "development" and (
            origin.scheme not in {"http", "https"}
            or origin.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Development origin must be loopback")
        if self.github_redirect_uri and self.github_redirect_uri != (
            self.app_origin + "/api/v1/auth/github/callback"
        ):
            raise ValueError("GITHUB_REDIRECT_URI must match the configured origin callback")
        if not 1 <= self.max_accounts <= 10000:
            raise ValueError("MAX_ACCOUNTS must be between 1 and 10000")

    @property
    def secure_cookie(self):
        return self.app_origin.startswith("https://")

    @property
    def cookie_name(self):
        return "__Host-signaldesk" if self.secure_cookie else "signaldesk_dev"

    @property
    def oauth_cookie_name(self):
        return "__Host-signaldesk-oauth" if self.secure_cookie else "signaldesk_oauth_dev"

    @property
    def oauth_enabled(self):
        return bool(
            self.github_client_id and self.github_client_secret and self.github_redirect_uri
        )

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            app_env=os.getenv("APP_ENV", "production"),
            app_origin=os.getenv("APP_ORIGIN", "https://localhost"),
            github_client_id=os.getenv("GITHUB_CLIENT_ID", ""),
            github_client_secret=os.getenv("GITHUB_CLIENT_SECRET", ""),
            github_redirect_uri=os.getenv("GITHUB_REDIRECT_URI", ""),
            max_accounts=int(os.getenv("MAX_ACCOUNTS", "100")),
        )
