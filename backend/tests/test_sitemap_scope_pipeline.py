from app.services.sitemap_fetcher import (
    SitemapEntry,
    classify_url_scope,
    is_obvious_non_page_url,
    select_resolution_entries,
)


def _entry(root: str, url: str) -> SitemapEntry:
    return SitemapEntry(
        url=url,
        child_sitemap=root,
        hostname='',
        scope_class=classify_url_scope(root, url),
        obvious_non_page=is_obvious_non_page_url(url),
    )


def test_scope_classification():
    root = 'https://www.example.com/sitemap.xml'
    assert classify_url_scope(root, 'https://www.example.com/a') == 'ROOT_HOST'
    assert classify_url_scope(root, 'https://blog.example.com/a') == 'RELATED_SUBDOMAIN'
    assert classify_url_scope(root, 'https://external.example.org/a') == 'EXTERNAL_DOMAIN'


def test_strict_host_keeps_only_root_host_pages():
    root = 'https://www.example.com/sitemap.xml'
    entries = [
        _entry(root, 'https://www.example.com/a'),
        _entry(root, 'https://blog.example.com/b'),
        _entry(root, 'https://external.example.org/c'),
    ]
    selected, stats = select_resolution_entries(entries, scope_mode='strict_host')
    assert [x.url for x in selected] == ['https://www.example.com/a']
    assert stats['excluded_by_scope_urls'] == 2


def test_registrable_domain_keeps_subdomains_but_excludes_external():
    root = 'https://www.example.com/sitemap.xml'
    entries = [
        _entry(root, 'https://www.example.com/a'),
        _entry(root, 'https://blog.example.com/b'),
        _entry(root, 'https://external.example.org/c'),
    ]
    selected, stats = select_resolution_entries(entries, scope_mode='registrable_domain')
    assert {x.url for x in selected} == {
        'https://www.example.com/a',
        'https://blog.example.com/b',
    }
    assert stats['excluded_external_domain_urls'] == 1


def test_sitemap_authoritative_keeps_external_declared_pages():
    root = 'https://www.example.com/sitemap.xml'
    entries = [
        _entry(root, 'https://www.example.com/a'),
        _entry(root, 'https://external.example.org/c'),
    ]
    selected, stats = select_resolution_entries(
        entries,
        scope_mode='sitemap_authoritative',
        exclude_non_pages=False,
    )
    assert len(selected) == 2
    assert stats['excluded_by_scope_urls'] == 0


def test_non_page_filter_is_explicit_and_reversible():
    root = 'https://www.example.com/sitemap.xml'
    pdf = _entry(root, 'https://www.example.com/report.pdf')
    html = _entry(root, 'https://www.example.com/report')

    selected, stats = select_resolution_entries(
        [pdf, html],
        scope_mode='registrable_domain',
        exclude_non_pages=True,
    )
    assert [x.url for x in selected] == [html.url]
    assert stats['excluded_non_page_urls'] == 1

    selected_all, stats_all = select_resolution_entries(
        [pdf, html],
        scope_mode='registrable_domain',
        exclude_non_pages=False,
    )
    assert len(selected_all) == 2
    assert stats_all['excluded_non_page_urls'] == 0
