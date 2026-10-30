from __future__ import annotations

import hashlib
import logging
import secrets

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import delete, select

from app.auth_config import get_auth_settings
from app.auth_models import AppSession, AuditEvent
from app.database import SessionLocal


logger = logging.getLogger("sitemind.auth")

settings = get_auth_settings()


@dataclass(frozen=True)
class CurrentUser:
    sub: str
    email: str
    name: str | None = None

    def as_dict(self) -> dict:
        return {
            "sub": self.sub,
            "email": self.email,
            "name": self.name,
        }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(
        raw_token.encode("utf-8")
    ).hexdigest()


def local_development_user() -> CurrentUser:
    return CurrentUser(
        sub="local-development",
        email="local-development@sitemind.invalid",
        name="Local Development",
    )


def create_application_session(
    *,
    user_sub: str,
    user_email: str,
    user_name: str | None,
) -> str:
    """
    Create a new server-side application session.

    The browser receives `raw_token`.
    PostgreSQL stores only SHA-256(raw_token).
    """

    raw_token = secrets.token_urlsafe(48)
    session_hash = _hash_session_token(raw_token)

    now = utcnow()

    expires_at = now + timedelta(
        seconds=settings.session_max_age_seconds
    )

    with SessionLocal() as db:
        # Opportunistic session cleanup during logins.
        db.execute(
            delete(AppSession).where(
                AppSession.expires_at <= now
            )
        )

        row = AppSession(
            session_hash=session_hash,
            user_sub=user_sub,
            user_email=user_email,
            user_name=user_name,
            created_at=now,
            expires_at=expires_at,
            last_seen_at=now,
        )

        db.add(row)
        db.commit()

    return raw_token


def destroy_application_session(
    raw_token: str | None,
) -> None:
    if not raw_token:
        return

    session_hash = _hash_session_token(raw_token)

    try:
        with SessionLocal() as db:
            db.execute(
                delete(AppSession).where(
                    AppSession.session_hash == session_hash
                )
            )
            db.commit()

    except Exception:
        logger.exception(
            "Unable to destroy SiteMind application session"
        )


def get_application_session_user(
    raw_token: str | None,
) -> CurrentUser | None:
    if not raw_token:
        return None

    session_hash = _hash_session_token(raw_token)

    now = utcnow()

    with SessionLocal() as db:
        row = db.get(
            AppSession,
            session_hash,
        )

        if row is None:
            return None

        expires_at = _as_utc(row.expires_at)

        if expires_at <= now:
            db.delete(row)
            db.commit()
            return None

        last_seen_at = _as_utc(row.last_seen_at)

        # Avoid writing to PostgreSQL for every polling request.
        if (now - last_seen_at).total_seconds() >= 300:
            row.last_seen_at = now
            db.commit()

        return CurrentUser(
            sub=row.user_sub,
            email=row.user_email,
            name=row.user_name,
        )


def get_request_user(
    request: Request,
) -> CurrentUser | None:
    if not settings.auth_enabled:
        return local_development_user()

    raw_token = request.cookies.get(
        settings.session_cookie_name
    )

    return get_application_session_user(
        raw_token
    )


def request_source_ip(
    request: Request,
) -> str | None:
    forwarded = request.headers.get(
        "x-forwarded-for"
    )

    if forwarded:
        # ALB/nginx append addresses to X-Forwarded-For.
        return forwarded.split(",", 1)[0].strip()[:100]

    if request.client:
        return str(request.client.host)[:100]

    return None


def record_api_audit_event(
    *,
    request: Request,
    user: CurrentUser,
    status_code: int,
) -> None:
    """
    Record a mutating API operation.

    Logging failure must never make the user's primary API request fail.
    """

    try:
        user_agent = request.headers.get(
            "user-agent"
        )

        if user_agent:
            user_agent = user_agent[:2000]

        row = AuditEvent(
            actor_sub=user.sub,
            actor_email=user.email,
            actor_name=user.name,
            method=request.method.upper(),
            path=request.url.path[:1000],
            status_code=int(status_code),
            source_ip=request_source_ip(request),
            user_agent=user_agent,
            created_at=utcnow(),
        )

        with SessionLocal() as db:
            db.add(row)
            db.commit()

    except Exception:
        logger.exception(
            "Unable to persist SiteMind audit event"
        )


def recent_audit_events(
    limit: int = 100,
) -> list[dict]:
    """
    Utility for operational troubleshooting.

    Not currently exposed as a public API endpoint.
    """

    safe_limit = max(
        1,
        min(
            int(limit),
            500,
        ),
    )

    with SessionLocal() as db:
        rows = (
            db.execute(
                select(AuditEvent)
                .order_by(
                    AuditEvent.created_at.desc()
                )
                .limit(safe_limit)
            )
            .scalars()
            .all()
        )

        return [
            {
                "id": row.id,
                "actor_sub": row.actor_sub,
                "actor_email": row.actor_email,
                "actor_name": row.actor_name,
                "method": row.method,
                "path": row.path,
                "status_code": row.status_code,
                "source_ip": row.source_ip,
                "created_at": row.created_at,
            }
            for row in rows
        ]