from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]

ROOT_ENV = PROJECT_ROOT / ".env"
LOCAL_ENV = BACKEND_ROOT / ".env"


class AuthSettings(BaseSettings):
    """
    SiteMind authentication configuration.

    These settings intentionally live separately from the existing SEO/crawler
    Settings class so authentication can be introduced without changing crawler,
    AI, sitemap, queue or worker configuration.
    """

    auth_enabled: bool = False

    # Public application origin.
    site_url: str = "http://localhost:5173"

    # Okta OpenID Connect configuration.
    okta_issuer: str | None = None
    okta_client_id: str | None = None
    okta_client_secret: str | None = None
    okta_scopes: str = "openid profile email"

    # Can be explicitly configured, otherwise derived from SITE_URL.
    okta_redirect_uri: str | None = None
    okta_logout_redirect_uri: str | None = None

    # Opaque SiteMind application session.
    session_secret: str = ""
    session_cookie_name: str = "sitemind_session"
    session_max_age_seconds: int = Field(
        default=8 * 60 * 60,
        ge=15 * 60,
        le=7 * 24 * 60 * 60,
    )

    # Short-lived signed cookie used only while performing the OIDC
    # authorization-code redirect/callback flow.
    oidc_state_cookie_name: str = "sitemind_oidc_state"
    oidc_state_max_age_seconds: int = Field(
        default=10 * 60,
        ge=60,
        le=30 * 60,
    )

    model_config = SettingsConfigDict(
        env_file=(
            str(ROOT_ENV),
            str(LOCAL_ENV),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def normalized_site_url(self) -> str:
        return self.site_url.rstrip("/")

    @property
    def discovery_url(self) -> str:
        issuer = (self.okta_issuer or "").rstrip("/")
        return f"{issuer}/.well-known/openid-configuration"

    @property
    def effective_redirect_uri(self) -> str:
        configured = (self.okta_redirect_uri or "").strip()

        if configured:
            return configured

        return f"{self.normalized_site_url}/auth/callback"

    @property
    def effective_logout_redirect_uri(self) -> str:
        configured = (self.okta_logout_redirect_uri or "").strip()

        if configured:
            return configured

        return f"{self.normalized_site_url}/"

    @property
    def secure_cookies(self) -> bool:
        return self.normalized_site_url.lower().startswith("https://")

    @model_validator(mode="after")
    def validate_production_auth(self):
        if not self.auth_enabled:
            return self

        missing = []

        if not (self.okta_issuer or "").strip():
            missing.append("OKTA_ISSUER")

        if not (self.okta_client_id or "").strip():
            missing.append("OKTA_CLIENT_ID")

        if not (self.okta_client_secret or "").strip():
            missing.append("OKTA_CLIENT_SECRET")

        if not (self.session_secret or "").strip():
            missing.append("SESSION_SECRET")

        if missing:
            raise ValueError(
                "AUTH_ENABLED=true but required configuration is missing: "
                + ", ".join(missing)
            )

        if len(self.session_secret) < 32:
            raise ValueError(
                "SESSION_SECRET must contain at least 32 characters "
                "when AUTH_ENABLED=true"
            )

        if not self.normalized_site_url.startswith("https://"):
            raise ValueError(
                "SITE_URL must use HTTPS when AUTH_ENABLED=true"
            )

        if not self.effective_redirect_uri.startswith("https://"):
            raise ValueError(
                "OKTA_REDIRECT_URI must use HTTPS when AUTH_ENABLED=true"
            )

        return self


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()