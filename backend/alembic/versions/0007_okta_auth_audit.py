"""SiteMind Okta sessions and API audit events

Revision ID: 0007_okta_auth_audit
Revises: 0006_sitemap_duplicate_audit
"""

from alembic import op

import sqlalchemy as sa


revision = "0007_okta_auth_audit"
down_revision = "0006_sitemap_duplicate_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_sessions",

        sa.Column(
            "session_hash",
            sa.String(length=64),
            nullable=False,
        ),

        sa.Column(
            "user_sub",
            sa.String(length=255),
            nullable=False,
        ),

        sa.Column(
            "user_email",
            sa.String(length=320),
            nullable=False,
        ),

        sa.Column(
            "user_name",
            sa.String(length=255),
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.PrimaryKeyConstraint(
            "session_hash"
        ),
    )

    op.create_index(
        "ix_app_sessions_user_sub",
        "app_sessions",
        ["user_sub"],
    )

    op.create_index(
        "ix_app_sessions_user_email",
        "app_sessions",
        ["user_email"],
    )

    op.create_index(
        "ix_app_sessions_expires_at",
        "app_sessions",
        ["expires_at"],
    )

    op.create_table(
        "audit_events",

        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
        ),

        sa.Column(
            "actor_sub",
            sa.String(length=255),
            nullable=False,
        ),

        sa.Column(
            "actor_email",
            sa.String(length=320),
            nullable=False,
        ),

        sa.Column(
            "actor_name",
            sa.String(length=255),
            nullable=True,
        ),

        sa.Column(
            "method",
            sa.String(length=10),
            nullable=False,
        ),

        sa.Column(
            "path",
            sa.String(length=1000),
            nullable=False,
        ),

        sa.Column(
            "status_code",
            sa.Integer(),
            nullable=False,
        ),

        sa.Column(
            "source_ip",
            sa.String(length=100),
            nullable=True,
        ),

        sa.Column(
            "user_agent",
            sa.Text(),
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.PrimaryKeyConstraint(
            "id"
        ),
    )

    op.create_index(
        "ix_audit_events_actor_sub",
        "audit_events",
        ["actor_sub"],
    )

    op.create_index(
        "ix_audit_events_actor_email",
        "audit_events",
        ["actor_email"],
    )

    op.create_index(
        "ix_audit_events_created_at",
        "audit_events",
        ["created_at"],
    )


def downgrade():
    op.drop_index(
        "ix_audit_events_created_at",
        table_name="audit_events",
    )

    op.drop_index(
        "ix_audit_events_actor_email",
        table_name="audit_events",
    )

    op.drop_index(
        "ix_audit_events_actor_sub",
        table_name="audit_events",
    )

    op.drop_table(
        "audit_events"
    )

    op.drop_index(
        "ix_app_sessions_expires_at",
        table_name="app_sessions",
    )

    op.drop_index(
        "ix_app_sessions_user_email",
        table_name="app_sessions",
    )

    op.drop_index(
        "ix_app_sessions_user_sub",
        table_name="app_sessions",
    )

    op.drop_table(
        "app_sessions"
    )