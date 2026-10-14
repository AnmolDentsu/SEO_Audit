from __future__ import annotations

import pytest

from app.services import security, sitemap_fetcher
from app.services.sitemap_classifier import classify_page


@pytest.mark.asyncio
async def test_nested_sitemap_indexes_are_exhausted(monkeypatch):
    docs = {
        "https://example.com/sitemap.xml": b'''<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap><loc>https://example.com/level-1.xml</loc></sitemap>
        </sitemapindex>''',
        "https://example.com/level-1.xml": b'''<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap><loc>https://example.com/level-2.xml</loc></sitemap>
        </sitemapindex>''',
        "https://example.com/level-2.xml": b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/a</loc></url>
            <url><loc>https://example.com/b</loc></url>
        </urlset>''',
    }

    async def fake_fetch(client, sitemap_url, *, dns_cache):
        body = docs[sitemap_url]
        return sitemap_url, 200, {"content-type": "application/xml"}, body, "http", 0

    monkeypatch.setattr(sitemap_fetcher, "_fetch_sitemap_document", fake_fetch)
    entries, issues = await sitemap_fetcher.discover_sitemap_urls(
        "https://example.com/sitemap.xml",
        max_urls=100,
        max_sitemaps=20,
    )

    assert [x.url for x in entries] == ["https://example.com/a", "https://example.com/b"]
    assert not [x for x in issues if x.get("severity") == "error"]


@pytest.mark.asyncio
async def test_xml_link_inside_urlset_is_explored(monkeypatch):
    docs = {
        "https://example.com/sitemap.xml": b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/nested-sitemap.xml</loc></url>
            <url><loc>https://example.com/normal-page</loc></url>
        </urlset>''',
        "https://example.com/nested-sitemap.xml": b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/from-nested</loc></url>
        </urlset>''',
    }

    async def fake_fetch(client, sitemap_url, *, dns_cache):
        body = docs[sitemap_url]
        return sitemap_url, 200, {"content-type": "application/xml"}, body, "http", 0

    monkeypatch.setattr(sitemap_fetcher, "_fetch_sitemap_document", fake_fetch)
    entries, _ = await sitemap_fetcher.discover_sitemap_urls(
        "https://example.com/sitemap.xml",
        max_urls=100,
        max_sitemaps=20,
    )

    urls = {x.url for x in entries}
    assert "https://example.com/normal-page" in urls
    assert "https://example.com/from-nested" in urls
    assert "https://example.com/nested-sitemap.xml" not in urls


@pytest.mark.asyncio
async def test_corporate_private_dns_is_allowed_only_after_public_dns_verification(monkeypatch):
    async def fake_system(host, port):
        return {"10.20.30.40"}

    async def fake_public(host):
        return {"93.184.216.34"}

    monkeypatch.setattr(security, "_resolve_system_ips", fake_system)
    monkeypatch.setattr(security, "_resolve_public_ips", fake_public)
    await security.validate_public_url("https://example.com/sitemap.xml")


@pytest.mark.asyncio
async def test_private_literal_is_still_blocked():
    with pytest.raises(ValueError):
        await security.validate_public_url("http://169.254.169.254/latest/meta-data/")


def test_generic_product_classifier_uses_real_page_evidence():
    evidence = {
        "final_url": "https://example.com/service/pro-plan",
        "title": "Pro Plan",
        "meta_description": "Choose a plan for your team.",
        "h1": ["Pro Plan"],
        "h2": ["Pricing", "Features", "What's included"],
        "schema_types": ["Service", "Offer"],
        "cta_texts": ["Get started"],
        "forms_count": 1,
        "word_count": 650,
        "main_internal_links_count": 8,
    }
    result = classify_page(evidence)
    assert result["detailed_page_type"] == "PRODUCT"
    assert result["confidence"] >= 0.87


def test_generic_blog_classifier_is_not_bfsi_specific():
    evidence = {
        "final_url": "https://example.com/insights/how-to-pack-for-a-long-flight",
        "title": "How to Pack for a Long Flight",
        "meta_description": "A practical guide for travellers.",
        "h1": ["How to Pack for a Long Flight"],
        "h2": ["Carry-on essentials", "What to avoid"],
        "schema_types": ["Article"],
        "author_present": True,
        "published_at_text": "2026-09-01",
        "article_element": True,
        "cta_texts": [],
        "forms_count": 0,
        "word_count": 1500,
        "main_internal_links_count": 12,
    }
    result = classify_page(evidence)
    assert result["detailed_page_type"] == "CONTENT"
    assert result["confidence"] >= 0.93
