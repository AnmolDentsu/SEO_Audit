from __future__ import annotations

import pytest

from app.services import sitemap_fetcher
from app.services.sitemap_job_runner import _discovery_status


ROOT = "https://example.com/sitemap.xml"
CHILD = "https://example.com/empty.xml"


def _root_index(child: str = CHILD) -> bytes:
    return f'''<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>{child}</loc></sitemap>
    </sitemapindex>'''.encode()


def _urlset(url: str) -> bytes:
    return f'''<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>{url}</loc></url>
    </urlset>'''.encode()


@pytest.mark.asyncio
async def test_empty_child_body_is_bounded_and_audited(monkeypatch):
    monkeypatch.setattr(sitemap_fetcher.settings, "sitemap_empty_child_verify_attempts", 2)
    monkeypatch.setattr(sitemap_fetcher.settings, "sitemap_empty_child_verify_delay_seconds", 0.0)
    calls = {ROOT: 0, CHILD: 0}

    async def fake_fetch(client, sitemap_url, *, dns_cache):
        calls[sitemap_url] += 1
        if sitemap_url == ROOT:
            return ROOT, 200, {"content-type": "application/xml"}, _root_index(), "http", 0
        return CHILD, 200, {"content-type": "application/xml"}, b"   \n", "http", 0

    monkeypatch.setattr(sitemap_fetcher, "_fetch_sitemap_document", fake_fetch)

    entries, issues, stats = await sitemap_fetcher.discover_sitemap_inventory(
        ROOT,
        max_urls=100,
        max_sitemaps=10,
    )

    assert entries == []
    empty = [x for x in issues if x.get("code") == "EMPTY_CHILD_SITEMAP"]
    assert len(empty) == 1
    assert empty[0]["sitemap"] == CHILD
    assert empty[0]["parent_sitemap"] == ROOT
    assert empty[0]["severity"] == "warning"
    assert empty[0]["empty_kind"] == "empty_body"
    assert empty[0]["verification_attempts"] >= 1
    assert calls[CHILD] == 2  # first observation + one confirming verification
    assert stats["empty_child_sitemaps"] == 1
    assert stats["failed_xmls"] == 0
    assert stats["root_document_parsed"] is True


@pytest.mark.asyncio
async def test_empty_child_that_becomes_non_empty_is_parsed_normally(monkeypatch):
    monkeypatch.setattr(sitemap_fetcher.settings, "sitemap_empty_child_verify_attempts", 2)
    monkeypatch.setattr(sitemap_fetcher.settings, "sitemap_empty_child_verify_delay_seconds", 0.0)
    child_calls = 0

    async def fake_fetch(client, sitemap_url, *, dns_cache):
        nonlocal child_calls
        if sitemap_url == ROOT:
            return ROOT, 200, {}, _root_index(), "http", 0
        child_calls += 1
        body = b"" if child_calls == 1 else _urlset("https://example.com/recovered")
        return CHILD, 200, {}, body, "http", 0

    monkeypatch.setattr(sitemap_fetcher, "_fetch_sitemap_document", fake_fetch)
    entries, issues, stats = await sitemap_fetcher.discover_sitemap_inventory(ROOT, max_urls=100, max_sitemaps=10)

    assert [x.url for x in entries] == ["https://example.com/recovered"]
    assert not [x for x in issues if x.get("code") == "EMPTY_CHILD_SITEMAP"]
    assert stats["empty_child_sitemaps"] == 0


@pytest.mark.asyncio
async def test_valid_empty_child_urlset_is_warning_not_error(monkeypatch):
    async def fake_fetch(client, sitemap_url, *, dns_cache):
        if sitemap_url == ROOT:
            return ROOT, 200, {}, _root_index(), "http", 0
        return CHILD, 200, {}, b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>', "http", 0

    monkeypatch.setattr(sitemap_fetcher, "_fetch_sitemap_document", fake_fetch)
    entries, issues, stats = await sitemap_fetcher.discover_sitemap_inventory(ROOT, max_urls=100, max_sitemaps=10)

    assert entries == []
    assert stats["empty_child_sitemaps"] == 1
    assert stats["failed_xmls"] == 0
    assert any(
        x.get("code") == "EMPTY_CHILD_SITEMAP"
        and x.get("empty_kind") == "empty_urlset"
        and x.get("severity") == "warning"
        for x in issues
    )


@pytest.mark.asyncio
async def test_empty_root_is_never_downgraded_to_empty_child_warning(monkeypatch):
    async def fake_fetch(client, sitemap_url, *, dns_cache):
        return ROOT, 200, {}, b"", "http", 0

    monkeypatch.setattr(sitemap_fetcher, "_fetch_sitemap_document", fake_fetch)
    entries, issues, stats = await sitemap_fetcher.discover_sitemap_inventory(ROOT, max_urls=100, max_sitemaps=10)

    assert entries == []
    assert stats["empty_child_sitemaps"] == 0
    assert stats["failed_xmls"] == 1
    assert not [x for x in issues if x.get("code") == "EMPTY_CHILD_SITEMAP"]
    assert any(x.get("code") == "SITEMAP_FETCH_FAILED" for x in issues)


def test_confirmed_empty_children_do_not_make_whole_source_partial():
    warnings = [{"code": "EMPTY_CHILD_SITEMAP", "severity": "warning"}]
    stats = {"empty_child_sitemaps": 1, "root_document_parsed": True}
    assert _discovery_status([], [], warnings, stats) == "warning"


def test_real_discovery_error_still_wins_over_empty_child_warning():
    errors = [{"code": "SITEMAP_FETCH_FAILED", "severity": "error"}]
    warnings = [{"code": "EMPTY_CHILD_SITEMAP", "severity": "warning"}]
    stats = {"empty_child_sitemaps": 1, "root_document_parsed": True}
    assert _discovery_status([], errors, warnings, stats) == "failed"
