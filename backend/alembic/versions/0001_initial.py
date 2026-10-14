"""initial schema"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.UniqueConstraint("project_id", "url", name="uq_project_target_url"),
    )
    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("keyword", sa.String(500), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("target_min", sa.Float(), nullable=True),
        sa.Column("target_max", sa.Float(), nullable=True),
        sa.UniqueConstraint("project_id", "keyword", name="uq_project_keyword"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("status", sa.String(30), nullable=False, index=True),
        sa.Column("phase", sa.String(50), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "page_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("from_cache", sa.Boolean(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("h1_json", sa.JSON(), nullable=False),
        sa.Column("h2_json", sa.JSON(), nullable=False),
        sa.Column("h3_json", sa.JSON(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_table(
        "density_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("page_result_id", sa.Integer(), sa.ForeignKey("page_results.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("keyword_id", sa.Integer(), sa.ForeignKey("keywords.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("density_pct", sa.Float(), nullable=False),
        sa.Column("present", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("page_result_id", "keyword_id", name="uq_page_keyword_density"),
    )
    op.create_table(
        "opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
    )
    op.create_table(
        "page_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade():
    op.drop_table("page_cache")
    op.drop_table("opportunities")
    op.drop_table("density_results")
    op.drop_table("page_results")
    op.drop_table("runs")
    op.drop_table("keywords")
    op.drop_table("targets")
    op.drop_table("projects")
