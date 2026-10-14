from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    client_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    parent: Mapped[Project | None] = relationship(
        "Project",
        remote_side="Project.id",
        back_populates="subprojects",
    )
    subprojects: Mapped[list[Project]] = relationship(
        "Project",
        back_populates="parent",
        passive_deletes=True,
    )
    targets: Mapped[list["Target"]] = relationship(
        cascade="all, delete-orphan",
        back_populates="project",
    )
    keywords: Mapped[list["Keyword"]] = relationship(
        cascade="all, delete-orphan",
        back_populates="project",
    )


class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (
        UniqueConstraint("project_id", "url", name="uq_project_target_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="OTHER")
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    project: Mapped[Project] = relationship(back_populates="targets")


class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = (
        UniqueConstraint("project_id", "keyword", name="uq_project_keyword"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    keyword: Mapped[str] = mapped_column(String(500))
    keyword_type: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        default="OTHER",
    )
    search_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Legacy fields retained temporarily for backward-compatible databases.
    # New runs derive Target Min/Max from URL roles P1/P0 per run.
    priority: Mapped[str] = mapped_column(String(20), default="NONE")
    target_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    project: Mapped[Project] = relationship(back_populates="keywords")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("project_id", "run_number", name="uq_project_run_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    # User-facing sequence. It starts from 1 independently for every project.
    # `id` remains the globally unique database/API identifier.
    run_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    phase: Mapped[str | None] = mapped_column(String(50), nullable=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analysis_json: Mapped[dict] = mapped_column(JSON, default=dict)


class PageResult(Base):
    __tablename__ = "page_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        index=True,
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    h1_json: Mapped[list] = mapped_column(JSON, default=list)
    h2_json: Mapped[list] = mapped_column(JSON, default=list)
    h3_json: Mapped[list] = mapped_column(JSON, default=list)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_root: Mapped[str | None] = mapped_column(String(50), nullable=True)
    paragraph_count: Mapped[int] = mapped_column(Integer, default=0)
    structure_json: Mapped[dict] = mapped_column(JSON, default=dict)


class DensityResult(Base):
    __tablename__ = "density_results"
    __table_args__ = (
        UniqueConstraint(
            "page_result_id",
            "keyword_id",
            name="uq_page_keyword_density",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_result_id: Mapped[int] = mapped_column(
        ForeignKey("page_results.id", ondelete="CASCADE"),
        index=True,
    )
    keyword_id: Mapped[int] = mapped_column(
        ForeignKey("keywords.id", ondelete="CASCADE"),
        index=True,
    )
    count: Mapped[int] = mapped_column(Integer, default=0)
    density_pct: Mapped[float] = mapped_column(Float, default=0.0)
    present: Mapped[bool] = mapped_column(Boolean, default=False)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        index=True,
    )
    category: Mapped[str] = mapped_column(String(80))
    priority: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(500))
    recommendation: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(30), default="deterministic")


class PageCache(Base):
    __tablename__ = "page_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SitemapSource(Base):
    __tablename__ = "sitemap_sources"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "root_url",
            name="uq_project_sitemap_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    root_url: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="COMPETITOR", index=True)
    label: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SitemapRun(Base):
    __tablename__ = "sitemap_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "run_number",
            name="uq_project_sitemap_run_number",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    # Independent project-local sequence for Sitemap Intelligence.
    run_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    phase: Mapped[str | None] = mapped_column(String(60), nullable=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=1000)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SitemapUrl(Base):
    __tablename__ = "sitemap_urls"
    __table_args__ = (
        UniqueConstraint(
            "sitemap_run_id",
            "normalized_url",
            name="uq_sitemap_run_normalized_url",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sitemap_run_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_runs.id", ondelete="CASCADE"),
        index=True,
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_sources.id", ondelete="CASCADE"),
        index=True,
    )
    root_sitemap: Mapped[str] = mapped_column(Text)
    child_sitemap: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text, index=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    lastmod: Mapped[str | None] = mapped_column(String(100), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetch_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    broad_page_type: Mapped[str] = mapped_column(String(30), default="OTHER", index=True)
    detailed_page_type: Mapped[str] = mapped_column(String(40), default="OTHER", index=True)
    type_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    classification_method: Mapped[str] = mapped_column(String(30), default="deterministic")
    classification_evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    category: Mapped[str] = mapped_column(String(120), default="Other / Unclassified", index=True)
    topic: Mapped[str] = mapped_column(String(200), default="Other / Unclassified", index=True)
    subtopic: Mapped[str | None] = mapped_column(String(250), nullable=True)
    topic_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    topic_method: Mapped[str] = mapped_column(String(30), default="deterministic")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    h1_json: Mapped[list] = mapped_column(JSON, default=list)
    h2_json: Mapped[list] = mapped_column(JSON, default=list)
    schema_types_json: Mapped[list] = mapped_column(JSON, default=list)
    breadcrumbs_json: Mapped[list] = mapped_column(JSON, default=list)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    author_present: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cta_json: Mapped[list] = mapped_column(JSON, default=list)
    text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SitemapDuplicate(Base):
    __tablename__ = "sitemap_duplicates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sitemap_run_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_runs.id", ondelete="CASCADE"),
        index=True,
    )
    primary_source_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_sources.id", ondelete="CASCADE"),
        index=True,
    )
    duplicate_source_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_sources.id", ondelete="CASCADE"),
        index=True,
    )
    duplicate_kind: Mapped[str] = mapped_column(String(50), index=True)
    audit_stage: Mapped[str] = mapped_column(String(40), index=True)
    group_key: Mapped[str] = mapped_column(String(64), index=True)
    match_url: Mapped[str] = mapped_column(Text)
    primary_url: Mapped[str] = mapped_column(Text)
    duplicate_url: Mapped[str] = mapped_column(Text)
    primary_child_sitemap: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_child_sitemap: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_lastmod: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duplicate_lastmod: Mapped[str | None] = mapped_column(String(100), nullable=True)
    primary_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SitemapOpportunity(Base):
    __tablename__ = "sitemap_opportunities"
    __table_args__ = (
        UniqueConstraint(
            "sitemap_run_id",
            "opportunity_key",
            name="uq_sitemap_opportunity_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sitemap_run_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_runs.id", ondelete="CASCADE"),
        index=True,
    )
    opportunity_key: Mapped[str] = mapped_column(String(100), index=True)
    gap_type: Mapped[str] = mapped_column(String(50), index=True)
    priority: Mapped[str] = mapped_column(String(20), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recommended_page_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    client_count: Mapped[int] = mapped_column(Integer, default=0)
    competitor_count: Mapped[int] = mapped_column(Integer, default=0)
    competitor_sites: Mapped[int] = mapped_column(Integer, default=0)
    business_relevance: Mapped[str] = mapped_column(String(30), default="uncertain")
    title: Mapped[str] = mapped_column(String(500))
    recommendation: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(40), default="deterministic")


class SitemapPageCache(Base):
    __tablename__ = "sitemap_page_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    normalized_url: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SitemapEmbeddingCache(Base):
    __tablename__ = "sitemap_embedding_cache"
    __table_args__ = (
        UniqueConstraint(
            "text_hash",
            "model_key",
            "dimension",
            name="uq_sitemap_embedding_cache",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    text_preview: Mapped[str | None] = mapped_column(String(300), nullable=True)
    model_key: Mapped[str] = mapped_column(String(120))
    dimension: Mapped[int] = mapped_column(Integer)
    vector_json: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

