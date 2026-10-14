"""sitemap intelligence

Revision ID: 0004_sitemap_intelligence
Revises: 0003_detailed_analysis
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_sitemap_intelligence"
down_revision = "0003_detailed_analysis"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sitemap_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("root_url", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="COMPETITOR"),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "root_url", name="uq_project_sitemap_source"),
    )
    op.create_index("ix_sitemap_sources_project_id", "sitemap_sources", ["project_id"])
    op.create_index("ix_sitemap_sources_role", "sitemap_sources", ["role"])

    op.create_table(
        "sitemap_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(length=60), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("analysis_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sitemap_runs_project_id", "sitemap_runs", ["project_id"])
    op.create_index("ix_sitemap_runs_status", "sitemap_runs", ["status"])

    op.create_table(
        "sitemap_urls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sitemap_run_id", sa.Integer(), sa.ForeignKey("sitemap_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sitemap_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("root_sitemap", sa.Text(), nullable=False),
        sa.Column("child_sitemap", sa.Text(), nullable=True),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("lastmod", sa.String(length=100), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("fetch_method", sa.String(length=30), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("broad_page_type", sa.String(length=30), nullable=False, server_default="OTHER"),
        sa.Column("detailed_page_type", sa.String(length=40), nullable=False, server_default="OTHER"),
        sa.Column("type_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("classification_method", sa.String(length=30), nullable=False, server_default="deterministic"),
        sa.Column("classification_evidence_json", sa.JSON(), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False, server_default="Other / Unclassified"),
        sa.Column("topic", sa.String(length=200), nullable=False, server_default="Other / Unclassified"),
        sa.Column("subtopic", sa.String(length=250), nullable=True),
        sa.Column("topic_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("topic_method", sa.String(length=30), nullable=False, server_default="deterministic"),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("h1_json", sa.JSON(), nullable=False),
        sa.Column("h2_json", sa.JSON(), nullable=False),
        sa.Column("schema_types_json", sa.JSON(), nullable=False),
        sa.Column("breadcrumbs_json", sa.JSON(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("author_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("published_at_text", sa.String(length=200), nullable=True),
        sa.Column("cta_json", sa.JSON(), nullable=False),
        sa.Column("text_excerpt", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("sitemap_run_id", "normalized_url", name="uq_sitemap_run_normalized_url"),
    )
    for name, cols in [
        ("ix_sitemap_urls_sitemap_run_id", ["sitemap_run_id"]),
        ("ix_sitemap_urls_source_id", ["source_id"]),
        ("ix_sitemap_urls_normalized_url", ["normalized_url"]),
        ("ix_sitemap_urls_broad_page_type", ["broad_page_type"]),
        ("ix_sitemap_urls_detailed_page_type", ["detailed_page_type"]),
        ("ix_sitemap_urls_category", ["category"]),
        ("ix_sitemap_urls_topic", ["topic"]),
    ]:
        op.create_index(name, "sitemap_urls", cols)

    op.create_table(
        "sitemap_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sitemap_run_id", sa.Integer(), sa.ForeignKey("sitemap_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opportunity_key", sa.String(length=100), nullable=False),
        sa.Column("gap_type", sa.String(length=50), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column("topic", sa.String(length=200), nullable=True),
        sa.Column("recommended_page_type", sa.String(length=40), nullable=True),
        sa.Column("client_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("competitor_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("competitor_sites", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("business_relevance", sa.String(length=30), nullable=False, server_default="uncertain"),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="deterministic"),
        sa.UniqueConstraint("sitemap_run_id", "opportunity_key", name="uq_sitemap_opportunity_key"),
    )
    op.create_index("ix_sitemap_opportunities_sitemap_run_id", "sitemap_opportunities", ["sitemap_run_id"])
    op.create_index("ix_sitemap_opportunities_opportunity_key", "sitemap_opportunities", ["opportunity_key"])
    op.create_index("ix_sitemap_opportunities_gap_type", "sitemap_opportunities", ["gap_type"])
    op.create_index("ix_sitemap_opportunities_priority", "sitemap_opportunities", ["priority"])

    op.create_table(
        "sitemap_page_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sitemap_page_cache_url_hash", "sitemap_page_cache", ["url_hash"])
    op.create_index("ix_sitemap_page_cache_expires_at", "sitemap_page_cache", ["expires_at"])

    op.create_table(
        "sitemap_embedding_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("text_preview", sa.String(length=300), nullable=True),
        sa.Column("model_key", sa.String(length=120), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("text_hash", "model_key", "dimension", name="uq_sitemap_embedding_cache"),
    )
    op.create_index("ix_sitemap_embedding_cache_text_hash", "sitemap_embedding_cache", ["text_hash"])


def downgrade():
    op.drop_table("sitemap_embedding_cache")
    op.drop_table("sitemap_page_cache")
    op.drop_table("sitemap_opportunities")
    op.drop_table("sitemap_urls")
    op.drop_table("sitemap_runs")
    op.drop_table("sitemap_sources")
