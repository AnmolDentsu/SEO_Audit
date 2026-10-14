"""persist sitemap duplicate and canonical collision audit

Revision ID: 0006_sitemap_duplicate_audit
Revises: 0005_project_controls
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_sitemap_duplicate_audit"
down_revision = "0005_project_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sitemap_duplicates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sitemap_run_id",
            sa.Integer(),
            sa.ForeignKey("sitemap_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "primary_source_id",
            sa.Integer(),
            sa.ForeignKey("sitemap_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "duplicate_source_id",
            sa.Integer(),
            sa.ForeignKey("sitemap_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("duplicate_kind", sa.String(length=50), nullable=False),
        sa.Column("audit_stage", sa.String(length=40), nullable=False),
        sa.Column("group_key", sa.String(length=64), nullable=False),
        sa.Column("match_url", sa.Text(), nullable=False),
        sa.Column("primary_url", sa.Text(), nullable=False),
        sa.Column("duplicate_url", sa.Text(), nullable=False),
        sa.Column("primary_child_sitemap", sa.Text(), nullable=True),
        sa.Column("duplicate_child_sitemap", sa.Text(), nullable=True),
        sa.Column("primary_final_url", sa.Text(), nullable=True),
        sa.Column("duplicate_final_url", sa.Text(), nullable=True),
        sa.Column("primary_canonical_url", sa.Text(), nullable=True),
        sa.Column("duplicate_canonical_url", sa.Text(), nullable=True),
        sa.Column("primary_lastmod", sa.String(length=100), nullable=True),
        sa.Column("duplicate_lastmod", sa.String(length=100), nullable=True),
        sa.Column("primary_http_status", sa.Integer(), nullable=True),
        sa.Column("duplicate_http_status", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index(
        "ix_sitemap_duplicates_sitemap_run_id",
        "sitemap_duplicates",
        ["sitemap_run_id"],
    )
    op.create_index(
        "ix_sitemap_duplicates_primary_source_id",
        "sitemap_duplicates",
        ["primary_source_id"],
    )
    op.create_index(
        "ix_sitemap_duplicates_duplicate_source_id",
        "sitemap_duplicates",
        ["duplicate_source_id"],
    )
    op.create_index(
        "ix_sitemap_duplicates_duplicate_kind",
        "sitemap_duplicates",
        ["duplicate_kind"],
    )
    op.create_index(
        "ix_sitemap_duplicates_audit_stage",
        "sitemap_duplicates",
        ["audit_stage"],
    )
    op.create_index(
        "ix_sitemap_duplicates_group_key",
        "sitemap_duplicates",
        ["group_key"],
    )
    op.create_index(
        "ix_sitemap_duplicates_run_kind",
        "sitemap_duplicates",
        ["sitemap_run_id", "duplicate_kind"],
    )
    op.create_index(
        "ix_sitemap_duplicates_run_stage",
        "sitemap_duplicates",
        ["sitemap_run_id", "audit_stage"],
    )


def downgrade():
    op.drop_table("sitemap_duplicates")
