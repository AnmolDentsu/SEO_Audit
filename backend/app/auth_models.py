from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AppSession(Base):
    """
    Server-side SiteMind application session.

    The browser receives only a random opaque token.

    The raw token is NEVER persisted in PostgreSQL. Only its SHA-256 hash is
    stored here.
    """

    __tablename__ = "app_sessions"

    session_hash: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    user_sub: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    user_email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        index=True,
    )

    user_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )


class AuditEvent(Base):
    """
    Attribution log for mutating SiteMind API requests.

    We intentionally do not persist request bodies because SEO inputs may be
    large and should not be duplicated into security logs.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    actor_sub: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    actor_email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        index=True,
    )

    actor_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    method: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    path: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
    )

    status_code: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    source_ip: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    user_agent: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
        index=True,
    )