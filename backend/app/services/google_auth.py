"""Google Gen AI / Vertex AI authentication provider.

Preferred production mode is Vertex AI with service-account identity or ADC.
GEMINI_API_KEY is supported only when AI_MODE=api_key; it is never combined
with service-account auth in a single client.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import google.auth
from google.oauth2 import service_account

from app.config import get_settings

settings = get_settings()
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass
class AuthSummary:
    ai_mode: str
    auth_source: str
    project: str | None
    location: str | None
    credentials_file: str | None = None
    api_key_configured: bool = False
    model: str | None = None
    embedding_model: str | None = None

    def safe_dict(self) -> dict:
        return asdict(self)


def _service_account_credentials(path: Path):
    if not path.exists() or not path.is_file():
        raise RuntimeError(
            f"GOOGLE_APPLICATION_CREDENTIALS points to a missing file: {path}. "
            "Use an absolute path or a path relative to the project root."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(path),
        scopes=[CLOUD_PLATFORM_SCOPE],
    )
    return creds


def build_genai_client():
    """Create a google-genai client for the configured mode.

    vertex:
      - If GOOGLE_APPLICATION_CREDENTIALS is set, load that service-account JSON.
      - Otherwise use Application Default Credentials (gcloud ADC locally or the
        attached service account on Cloud Run/GCE/GKE).
    api_key:
      - Use GEMINI_API_KEY with the Gemini Developer API.
    """
    from google import genai
    from google.genai import types

    mode = settings.ai_mode.strip().lower()

    if mode == "api_key":
        if not settings.gemini_api_key:
            raise RuntimeError("AI_MODE=api_key requires GEMINI_API_KEY.")
        return genai.Client(api_key=settings.gemini_api_key)

    if mode != "vertex":
        raise RuntimeError(f"AI client requested while AI_MODE={settings.ai_mode!r}.")

    project = settings.google_cloud_project
    credentials = None
    credentials_path = settings.resolve_credentials_path()
    auth_mode = settings.vertex_auth_mode.strip().lower()

    if auth_mode not in {"auto", "service_account", "adc"}:
        raise RuntimeError("VERTEX_AUTH_MODE must be auto, service_account, or adc.")

    if auth_mode in {"auto", "service_account"} and credentials_path:
        credentials = _service_account_credentials(credentials_path)
        project = project or getattr(credentials, "project_id", None)
    elif auth_mode == "service_account" and not credentials_path:
        raise RuntimeError(
            "VERTEX_AUTH_MODE=service_account requires GOOGLE_APPLICATION_CREDENTIALS."
        )
    else:
        # ADC resolves `gcloud auth application-default login` locally or the
        # runtime service account when deployed on Google Cloud.
        credentials, detected_project = google.auth.default(
            scopes=[CLOUD_PLATFORM_SCOPE]
        )
        project = project or detected_project

    if not project:
        raise RuntimeError(
            "No GCP project resolved. Set GCP_PROJECT_ID/GOOGLE_CLOUD_PROJECT or "
            "use credentials associated with a quota project."
        )

    return genai.Client(
        vertexai=True,
        project=project,
        location=settings.google_cloud_location,
        credentials=credentials,
        http_options=types.HttpOptions(api_version="v1"),
    )


def auth_summary() -> AuthSummary:
    mode = settings.ai_mode.strip().lower()
    if mode == "off":
        source = "disabled"
    elif mode == "mock":
        source = "mock"
    elif mode == "api_key":
        source = "gemini_api_key"
    elif mode == "vertex":
        credentials_path = settings.resolve_credentials_path()
        if credentials_path and settings.vertex_auth_mode.lower() != "adc":
            source = "service_account_file"
        else:
            source = "application_default_credentials"
    else:
        source = "invalid"

    credentials_path = settings.resolve_credentials_path()
    return AuthSummary(
        ai_mode=mode,
        auth_source=source,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location if mode == "vertex" else None,
        credentials_file=(credentials_path.name if credentials_path else None),
        api_key_configured=bool(settings.gemini_api_key),
        model=settings.gemini_model,
        embedding_model=settings.embedding_model,
    )


def probe_vertex_or_api_key() -> dict:
    """Perform a minimal model call to verify the configured AI auth path."""
    client = build_genai_client()
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents="Reply with exactly: OK",
    )
    return {
        **auth_summary().safe_dict(),
        "ok": True,
        "response": (response.text or "").strip()[:100],
    }
