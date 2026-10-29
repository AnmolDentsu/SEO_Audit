from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuth

from fastapi import APIRouter, HTTPException, Request

from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
)

from app.auth import (
    create_application_session,
    destroy_application_session,
    get_request_user,
)

from app.auth_config import (
    get_auth_settings,
)


logger = logging.getLogger(
    "sitemind.auth.routes"
)

settings = get_auth_settings()

router = APIRouter(
    prefix="/auth",
    tags=["authentication"],
)


oauth = OAuth()


if settings.auth_enabled:
    oauth.register(
        name="okta",
        client_id=settings.okta_client_id,
        client_secret=settings.okta_client_secret,
        server_metadata_url=settings.discovery_url,
        client_kwargs={
            "scope": settings.okta_scopes,
        },
    )


def _okta_client():
    client = oauth.create_client(
        "okta"
    )

    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Okta authentication is not configured",
        )

    return client


@router.get("/login")
async def login(
    request: Request,
):
    """
    Start Okta Authorization Code flow.
    """

    if not settings.auth_enabled:
        return RedirectResponse(
            url=settings.normalized_site_url,
            status_code=302,
        )

    client = _okta_client()

    response = await client.authorize_redirect(
        request,
        settings.effective_redirect_uri,
    )

    response.headers["Cache-Control"] = (
        "no-store"
    )

    return response


@router.get("/callback")
async def callback(
    request: Request,
):
    """
    Okta redirects the browser here with the authorization code.

    Authlib validates:
    - state
    - OIDC token response
    - ID token signature/claims
    """

    if not settings.auth_enabled:
        return RedirectResponse(
            url=settings.normalized_site_url,
            status_code=302,
        )

    client = _okta_client()

    try:
        token = await client.authorize_access_token(
            request
        )

    except Exception:
        logger.exception(
            "Okta authorization-code exchange failed"
        )

        raise HTTPException(
            status_code=401,
            detail="Okta authentication failed",
        )

    userinfo = token.get(
        "userinfo"
    )

    if not userinfo:
        logger.error(
            "Okta token response did not contain OIDC user information"
        )

        raise HTTPException(
            status_code=401,
            detail="Okta identity information was not returned",
        )

    user_sub = str(
        userinfo.get("sub") or ""
    ).strip()

    user_email = str(
        userinfo.get("email")
        or userinfo.get("preferred_username")
        or ""
    ).strip().lower()

    user_name = str(
        userinfo.get("name")
        or userinfo.get("preferred_username")
        or user_email
        or ""
    ).strip()

    if not user_sub:
        raise HTTPException(
            status_code=401,
            detail="Okta identity does not contain a subject identifier",
        )

    if not user_email:
        raise HTTPException(
            status_code=401,
            detail=(
                "Okta identity does not contain an email address. "
                "The SiteMind application requires the email claim."
            ),
        )

    if userinfo.get("email_verified") is False:
        raise HTTPException(
            status_code=403,
            detail="Okta email address is not verified",
        )

    raw_session_token = (
        create_application_session(
            user_sub=user_sub,
            user_email=user_email,
            user_name=user_name or None,
        )
    )

    # OIDC transaction state is no longer required after a successful callback.
    request.session.clear()

    response = RedirectResponse(
        url=settings.normalized_site_url,
        status_code=302,
    )

    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_session_token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )

    response.headers["Cache-Control"] = (
        "no-store"
    )

    return response


@router.get("/me")
async def me(
    request: Request,
):
    """
    Lightweight bootstrap endpoint used by the React AuthGate.
    """

    user = get_request_user(
        request
    )

    if user is None:
        response = JSONResponse(
            {
                "authenticated": False,
                "user": None,
            }
        )

    else:
        response = JSONResponse(
            {
                "authenticated": True,
                "user": user.as_dict(),
            }
        )

    response.headers["Cache-Control"] = (
        "no-store"
    )

    return response


@router.post("/logout")
async def logout(
    request: Request,
):
    """
    End only the SiteMind application session.

    The user's global corporate Okta session is intentionally left alone.
    Logging into SiteMind again may therefore complete through SSO without
    asking for credentials again.
    """

    if settings.auth_enabled:
        origin = request.headers.get(
            "origin"
        )

        if (
            origin
            and origin.rstrip("/")
            != settings.normalized_site_url
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid logout origin",
            )

        raw_token = request.cookies.get(
            settings.session_cookie_name
        )

        destroy_application_session(
            raw_token
        )

    request.session.clear()

    response = JSONResponse(
        {
            "ok": True,
        }
    )

    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
    )

    response.headers["Cache-Control"] = (
        "no-store"
    )

    return response