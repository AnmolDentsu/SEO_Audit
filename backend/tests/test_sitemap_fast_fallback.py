from app.services.sitemap_fallback import (
    select_browser_fallback_candidates,
    url_family_key,
)


def test_url_family_groups_dynamic_slugs():
    a = url_family_key("https://example.com/jobs/job-details/senior-data-engineer-bangalore-india")
    b = url_family_key("https://example.com/jobs/job-details/product-manager-mumbai-india")
    assert a == b
    assert a.endswith("/jobs/job-details/{slug}")


def test_browser_fallback_samples_same_family():
    items = [
        {
            "item_id": i,
            "requested_url": f"https://example.com/jobs/job-details/role-{i}-long-slug-example-test",
            "final_url": f"https://example.com/jobs/job-details/role-{i}-long-slug-example-test",
            "status_code": 403,
            "error": "HTTP 403",
            "terminal_state": "fetch_failed",
        }
        for i in range(20)
    ]
    selected, stats = select_browser_fallback_candidates(
        items,
        max_pages=100,
        per_host_cap=100,
        per_family_cap=3,
        stale_cap=0,
    )
    assert len(selected) == 3
    assert stats["skipped_family_cap"] == 17


def test_transient_5xx_does_not_consume_chromium():
    items = [
        {
            "item_id": 1,
            "requested_url": "https://example.com/a",
            "final_url": "https://example.com/a",
            "status_code": 503,
            "error": "HTTP 503",
            "terminal_state": "fetch_failed",
            "retryable": True,
        }
    ]
    selected, stats = select_browser_fallback_candidates(items)
    assert selected == []
    assert stats["skipped_transient_http"] == 1


def test_429_is_rate_controlled_not_impersonation_fallback():
    from app.services.sitemap_fallback import should_impersonation_fallback

    evidence = {
        "status_code": 429,
        "error": "HTTP 429",
        "terminal_state": "fetch_failed",
        "retryable": True,
    }
    assert should_impersonation_fallback(evidence) is False
