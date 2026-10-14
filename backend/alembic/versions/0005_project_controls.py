"""project controls, immutable inputs, and project-local run numbering

Revision ID: 0005_project_controls
Revises: 0004_sitemap_intelligence
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_project_controls"
down_revision = "0004_sitemap_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    # ------------------------------------------------------------------
    # Project hierarchy
    # ------------------------------------------------------------------
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("parent_project_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_projects_parent_project_id",
            "projects",
            ["parent_project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_projects_parent_project_id",
            ["parent_project_id"],
            unique=False,
        )

    # ------------------------------------------------------------------
    # Soft-disable mutable project inputs.
    # Historical PageResult / DensityResult rows must never be deleted just
    # because the user changes the current project configuration.
    # ------------------------------------------------------------------
    with op.batch_alter_table("targets") as batch:
        batch.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.create_index("ix_targets_enabled", ["enabled"], unique=False)

    with op.batch_alter_table("keywords") as batch:
        batch.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.create_index("ix_keywords_enabled", ["enabled"], unique=False)

    # ------------------------------------------------------------------
    # User-facing run numbers are local to each project.
    # Database primary keys remain globally unique and continue to be used by
    # API URLs / foreign keys.
    # ------------------------------------------------------------------
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("run_number", sa.Integer(), nullable=True))

    with op.batch_alter_table("sitemap_runs") as batch:
        batch.add_column(sa.Column("run_number", sa.Integer(), nullable=True))

    # Cross-compatible backfill for SQLite and PostgreSQL.
    op.execute(
        """
        UPDATE runs
        SET run_number = (
            SELECT COUNT(*)
            FROM runs AS older
            WHERE older.project_id = runs.project_id
              AND older.id <= runs.id
        )
        """
    )
    op.execute(
        """
        UPDATE sitemap_runs
        SET run_number = (
            SELECT COUNT(*)
            FROM sitemap_runs AS older
            WHERE older.project_id = sitemap_runs.project_id
              AND older.id <= sitemap_runs.id
        )
        """
    )

    with op.batch_alter_table("runs") as batch:
        batch.alter_column(
            "run_number",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_project_run_number",
            ["project_id", "run_number"],
        )

    with op.batch_alter_table("sitemap_runs") as batch:
        batch.alter_column(
            "run_number",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_project_sitemap_run_number",
            ["project_id", "run_number"],
        )


def downgrade():
    with op.batch_alter_table("sitemap_runs") as batch:
        batch.drop_constraint(
            "uq_project_sitemap_run_number",
            type_="unique",
        )
        batch.drop_column("run_number")

    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint(
            "uq_project_run_number",
            type_="unique",
        )
        batch.drop_column("run_number")

    with op.batch_alter_table("keywords") as batch:
        batch.drop_index("ix_keywords_enabled")
        batch.drop_column("enabled")

    with op.batch_alter_table("targets") as batch:
        batch.drop_index("ix_targets_enabled")
        batch.drop_column("enabled")

    with op.batch_alter_table("projects") as batch:
        batch.drop_index("ix_projects_parent_project_id")
        batch.drop_constraint(
            "fk_projects_parent_project_id",
            type_="foreignkey",
        )
        batch.drop_column("parent_project_id")
