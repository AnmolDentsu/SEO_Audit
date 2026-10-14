from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.analysis_contract import strip_brand_scope
from app.services import sitemap_page_resolver as resolver
from app import worker


def test_brand_scope_removed_recursively_without_removing_brand_content():
    payload = {
        "section_comparison": [
            {
                "Brand Scope": "BRAND",
                "Topic": "ICICI Prudential term insurance",
                "Brand Name": "ICICI Prudential",
            }
        ],
        "nested": {
            "brand_scope": "NON_BRAND",
            "Brand Scope Filter": "ALL",
            "ordinary_brand_copy": "brand awareness article",
        },
    }

    cleaned = strip_brand_scope(payload)

    assert "Brand Scope" not in cleaned["section_comparison"][0]
    assert cleaned["section_comparison"][0]["Brand Name"] == "ICICI Prudential"
    assert "brand_scope" not in cleaned["nested"]
    assert "Brand Scope Filter" not in cleaned["nested"]
    assert cleaned["nested"]["ordinary_brand_copy"] == "brand awareness article"


def test_content_rich_page_with_captcha_widget_is_not_a_challenge():
    paragraphs = " ".join(
        ["Useful tax planning article content for readers."] * 90
    )

    html = f"""
    <html>
      <head>
        <title>How to file income tax returns online</title>
        <script src="https://www.google.com/recaptcha/api.js"></script>
      </head>
      <body>
        <main>
          <article>
            <h1>How to file ITR online?</h1>
            <h2>Documents required</h2>
            <h2>Steps to file online</h2>
            <p>{paragraphs}</p>
          </article>
          <form><div class="g-recaptcha"></div></form>
        </main>
      </body>
    </html>
    """

    result = resolver._extract_evidence(
        html,
        "https://example.com/itr",
        200,
        "http",
        0,
    )

    assert result["word_count"] >= 280
    assert result["security_widget_detected"] is True
    assert result["challenge_detected"] is False
    assert result["error"] is None
    assert result["terminal_state"] == "usable"


def test_real_sparse_challenge_is_detected():
    html = """
    <html><head><title>Just a moment...</title></head>
    <body>
      <h1>Verify you are human</h1>
      <p>Checking your browser before accessing this website.</p>
    </body></html>
    """

    result = resolver._extract_evidence(
        html,
        "https://example.com/x",
        200,
        "http",
        0,
    )

    assert result["challenge_detected"] is True
    assert result["terminal_state"] == "security_challenge"
    assert result["error"]


def test_http_403_is_left_for_host_level_recovery(monkeypatch):
    async def fake_http(client, url, *, dns_cache):
        return {
            "final_url": url,
            "status_code": 403,
            "error": "HTTP 403",
            "fetch_method": "http",
            "retry_count": 0,
            "terminal_state": "fetch_failed",
            "retryable": False,
        }

    monkeypatch.setattr(
        resolver,
        "_safe_fetch_html",
        fake_http,
    )

    result = asyncio.run(
        resolver.resolve_pages_http(
            ["https://example.com/protected"],
            concurrency=1,
        )
    )[0]

    assert result["status_code"] == 403
    assert result["fetch_method"] == "http"
    assert result["terminal_state"] == "fetch_failed"
    assert result["error"] == "HTTP 403"

    # curl-cffi recovery is deliberately NOT performed inline here.
    # Repeated access blocks are handled by the host-level circuit and
    # the persisted recovery stage instead.
    assert result.get("recovered_by_impersonation") is not True


def test_worker_uses_phase_specific_watchdogs():
    assert (
        worker._stale_threshold_for_phase("browser_fallback")
        == worker.BROWSER_STALE_SECONDS
    )
    assert (
        worker._stale_threshold_for_phase("coverage_analysis")
        == worker.LONG_STAGE_STALE_SECONDS
    )
    assert (
        worker._stale_threshold_for_phase("page_resolution")
        == worker.PAGE_RESOLUTION_STALE_SECONDS
    )


def test_page_resolution_watchdog_scales_with_remaining_work():
    small = SimpleNamespace(
        phase="page_resolution",
        status="running",
        config_json={
            "_progress": {
                "stage_current": 100,
                "stage_total": 200,
            },
            "_sitemap_resolution": {
                "uncached_total": 100,
                "processed_uncached": 50,
            },
        },
    )

    large = SimpleNamespace(
        phase="page_resolution",
        status="running",
        config_json={
            "_progress": {
                "stage_current": 100,
                "stage_total": 20000,
            },
            "_sitemap_resolution": {
                "uncached_total": 15583,
                "processed_uncached": 0,
            },
        },
    )

    small_threshold = worker._stale_threshold_for_row(small)
    large_threshold = worker._stale_threshold_for_row(large)

    assert small_threshold >= worker.PAGE_RESOLUTION_STALE_SECONDS
    assert large_threshold > small_threshold
    assert large_threshold <= worker.PAGE_RESOLUTION_MAX_STALE_SECONDS


def test_cancelled_is_terminal_for_both_run_types():
    assert "cancelled" in worker._terminal_statuses("page")
    assert "cancelled" in worker._terminal_statuses("sitemap")
