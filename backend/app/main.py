from __future__ import annotations

import logging

from fastapi import (
    FastAPI,
    Request,
)

from fastapi.middleware.cors import (
    CORSMiddleware,
)

from fastapi.responses import (
    JSONResponse,
)

from sqlalchemy import text

from starlette.middleware.sessions import (
    SessionMiddleware,
)

from app.api import router
from app.auth import (
    get_request_user,
    record_api_audit_event,
)
from app.auth_config import (
    get_auth_settings,
)
from app.auth_routes import (
    router as auth_router,
)
from app.config import get_settings
from app.database import engine
from app.queue_api import (
    router as queue_router,
)


settings = get_settings()
auth_settings = get_auth_settings()


logging.basicConfig(
    level=getattr(
        logging,
        settings.log_level.upper(),
        logging.INFO,
    ),
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(name)s "
        "%(message)s"
    ),
)


app = FastAPI(
    title="SiteMind - SEO AI Platform API",
    version="0.1.0",

    # Do not expose API documentation anonymously in production.
    docs_url=(
        None
        if auth_settings.auth_enabled
        else "/docs"
    ),
    redoc_url=(
        None
        if auth_settings.auth_enabled
        else "/redoc"
    ),
    openapi_url=(
        None
        if auth_settings.auth_enabled
        else "/openapi.json"
    ),
)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Short-lived signed OIDC transaction state.
#
# This is NOT the user's SiteMind application session. The real application
# session is an opaque random cookie backed by PostgreSQL.
# ---------------------------------------------------------------------------

_oidc_state_secret = (
    auth_settings.session_secret
    or "local-development-oidc-state-secret"
)

app.add_middleware(
    SessionMiddleware,
    secret_key=_oidc_state_secret,
    session_cookie=auth_settings.oidc_state_cookie_name,
    max_age=auth_settings.oidc_state_max_age_seconds,
    same_site="lax",
    https_only=auth_settings.secure_cookies,
)


_MUTATING_METHODS = {
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
}


def _is_business_api(
    path: str,
) -> bool:
    return (
        path == "/api"
        or path.startswith("/api/")
    )


@app.middleware("http")
async def authenticate_and_audit_api(
    request: Request,
    call_next,
):
    """
    Protect all /api/* routes without modifying the existing large API modules.

    All approved Okta users intentionally retain equal application permissions.

    Every mutating API request is attributed to the authenticated Okta identity.
    """

    path = request.url.path

    # Local development / unit tests preserve the existing behaviour.
    if not auth_settings.auth_enabled:
        return await call_next(
            request
        )

    if not _is_business_api(path):
        return await call_next(
            request
        )

    # CORS preflight must not require an application session.
    if request.method.upper() == "OPTIONS":
        return await call_next(
            request
        )

    user = get_request_user(
        request
    )

    if user is None:
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Authentication required",
                "login_url": "/auth/login",
            },
            headers={
                "Cache-Control": "no-store",
            },
        )

    request.state.user = user

    try:
        response = await call_next(
            request
        )

    except Exception:
        if (
            request.method.upper()
            in _MUTATING_METHODS
        ):
            record_api_audit_event(
                request=request,
                user=user,
                status_code=500,
            )

        raise

    if (
        request.method.upper()
        in _MUTATING_METHODS
    ):
        record_api_audit_event(
            request=request,
            user=user,
            status_code=response.status_code,
        )

    return response


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(
    auth_router
)

app.include_router(
    router
)

app.include_router(
    queue_router
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "service": "seo-ai-platform-api",
    }


@app.get("/readyz")
def readyz():
    """
    ALB readiness probe.

    PostgreSQL failure must return HTTP 503, otherwise the ALB could continue
    considering an application with a dead database healthy.
    """

    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT 1"
                )
            )

        return {
            "status": "ready",
            "database": "ok",
            "ai_mode": settings.ai_mode,
        }

    except Exception:
        logging.getLogger(
            "sitemind.readiness"
        ).exception(
            "Database readiness check failed"
        )

        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "error",
            },
        )