from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base
from app.services import sitemap_fetcher
from app.services.sitemap_duplicate_audit import (
    CANONICAL_COLLISION,
    REDIRECT_COLLISION,
    canonical_state,
    resolved_collision_kind,
)
from app.services.sitemap_result_views import canonical_audit_page, duplicate_audit_page


@pytest.mark.asyncio
async def test_discovery_returns_exact_and_normalized_duplicate_evidence(monkeypatch):
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a</loc><lastmod>2026-09-01</lastmod></url>
      <url><loc>https://example.com/a</loc><lastmod>2026-09-02</lastmod></url>
      <url><loc>https://EXAMPLE.com/a/</loc><lastmod>2026-09-03</lastmod></url>
    </urlset>
    """

    async def fake_fetch(client, sitemap_url, *, dns_cache):
        return sitemap_url, 200, {}, xml, "http", 0

    monkeypatch.setattr(sitemap_fetcher, "_fetch_sitemap_document", fake_fetch)

    entries, issues, stats = await sitemap_fetcher.discover_sitemap_inventory(
        "https://example.com/sitemap.xml",
        max_urls=100,
        max_sitemaps=10,
    )

    assert issues == []
    assert len(entries) == 1
    assert stats["raw_url_records"] == 3
    assert stats["unique_sitemap_urls"] == 1
    assert stats["duplicate_url_records"] == 2

    evidence = stats["duplicate_evidence"]
    assert [x["duplicate_kind"] for x in evidence] == [
        "SITEMAP_EXACT_DUPLICATE",
        "SITEMAP_NORMALIZED_DUPLICATE",
    ]
    assert all(x["match_url"] == "https://example.com/a" for x in evidence)


def test_collision_kind_prefers_canonical_then_redirect():
    canonical = resolved_collision_kind(
        {
            "original_url": "https://example.com/a",
            "final_url": "https://example.com/a",
            "canonical_url": "https://example.com/target",
        },
        {
            "original_url": "https://example.com/b",
            "final_url": "https://example.com/b",
            "canonical_url": "https://example.com/target",
        },
        "https://example.com/target",
    )
    assert canonical == CANONICAL_COLLISION

    redirect = resolved_collision_kind(
        {
            "original_url": "https://example.com/a",
            "final_url": "https://example.com/target",
            "canonical_url": None,
        },
        {
            "original_url": "https://example.com/b",
            "final_url": "https://example.com/target",
            "canonical_url": None,
        },
        "https://example.com/target",
    )
    assert redirect == REDIRECT_COLLISION


def test_canonical_state_classification():
    assert canonical_state(
        SimpleNamespace(
            original_url="https://example.com/a",
            final_url="https://example.com/a",
            canonical_url=None,
        )
    ) == "MISSING"

    assert canonical_state(
        SimpleNamespace(
            original_url="https://example.com/a",
            final_url="https://example.com/a",
            canonical_url="https://example.com/a",
        )
    ) == "SELF_CANONICAL"

    assert canonical_state(
        SimpleNamespace(
            original_url="https://example.com/a",
            final_url="https://example.com/a",
            canonical_url="https://example.com/b",
        )
    ) == "CANONICAL_TO_OTHER"

    assert canonical_state(
        SimpleNamespace(
            original_url="https://example.com/a",
            final_url="https://example.com/a",
            canonical_url="https://other.example.net/a",
        )
    ) == "CROSS_HOST_CANONICAL"


def test_duplicate_and_canonical_result_views_are_paginated_and_auditable():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        project = models.Project(name="Audit Project")
        db.add(project)
        db.flush()

        client = models.SitemapSource(
            project_id=project.id,
            root_url="https://example.com/sitemap.xml",
            role="CLIENT",
            label="Client",
            enabled=True,
        )
        db.add(client)
        db.flush()

        run = models.SitemapRun(
            project_id=project.id,
            run_number=1,
            status="completed",
            config_json={
                "_input_snapshot": {
                    "sources": [
                        {
                            "id": client.id,
                            "root_url": client.root_url,
                            "role": client.role,
                            "label": client.label,
                            "enabled": True,
                        }
                    ]
                },
                "_duplicate_audit": {"version": 1, "mode": "native"},
            },
            analysis_json={},
        )
        db.add(run)
        db.flush()

        page = models.SitemapUrl(
            sitemap_run_id=run.id,
            source_id=client.id,
            root_sitemap=client.root_url,
            child_sitemap=client.root_url,
            original_url="https://example.com/a",
            normalized_url="https://example.com/a",
            final_url="https://example.com/a",
            canonical_url="https://example.com/a",
            http_status=200,
            fetch_method="http",
            broad_page_type="CONTENT",
            detailed_page_type="CONTENT",
            type_confidence=0.9,
            classification_method="deterministic",
            classification_evidence_json={},
            category="Knowledge",
            topic="Audit",
            topic_confidence=0.9,
            topic_method="deterministic",
            h1_json=[],
            h2_json=[],
            schema_types_json=[],
            breadcrumbs_json=[],
            word_count=100,
            author_present=False,
            cta_json=[],
        )
        db.add(page)

        duplicate = models.SitemapDuplicate(
            sitemap_run_id=run.id,
            primary_source_id=client.id,
            duplicate_source_id=client.id,
            duplicate_kind="SITEMAP_EXACT_DUPLICATE",
            audit_stage="sitemap_discovery",
            group_key="g" * 64,
            match_url="https://example.com/a",
            primary_url="https://example.com/a",
            duplicate_url="https://example.com/a",
        )
        db.add(duplicate)
        db.commit()

        duplicate_page = duplicate_audit_page(db, run.id, page=1, page_size=10)
        assert duplicate_page["total"] == 1
        assert duplicate_page["summary"]["duplicate_records"] == 1
        assert duplicate_page["summary"]["sitemap_exact_duplicates"] == 1
        assert duplicate_page["summary"]["evidence_status"] == "complete"
        assert duplicate_page["items"][0]["primary_site"] == "Client"

        canonical_page = canonical_audit_page(db, run.id, page=1, page_size=10)
        assert canonical_page["total"] == 1
        assert canonical_page["summary"]["canonical_present"] == 1
        assert canonical_page["items"][0]["canonical_state"] == "SELF_CANONICAL"
    finally:
        db.close()
