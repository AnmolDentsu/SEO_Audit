from types import SimpleNamespace

from app.services.sitemap_analysis import build_sitemap_analysis
from app.services.sitemap_classifier import classify_page


def _row(row_id, source_id, *, category, topic, page_type="CONTENT", status=200, error=None):
    return SimpleNamespace(
        id=row_id,
        source_id=source_id,
        error=error,
        broad_page_type=page_type,
        detailed_page_type=page_type,
        category=category,
        topic=topic,
        final_url=f"https://site{source_id}.example/page-{row_id}",
        original_url=f"https://site{source_id}.example/page-{row_id}",
        title=topic,
        h1_json=[topic],
        word_count=900,
        http_status=status,
        retry_count=0,
        classification_evidence_json={},
    )


def test_one_competitor_can_create_topic_gap():
    client = SimpleNamespace(id=1, role="CLIENT", label="Client", root_url="https://client.example/sitemap.xml")
    competitor = SimpleNamespace(id=2, role="COMPETITOR", label="Competitor", root_url="https://competitor.example/sitemap.xml")
    rows = [
        _row(1, 1, category="Insurance", topic="Life Insurance", page_type="PRODUCT"),
        _row(2, 2, category="Insurance", topic="Term Insurance", page_type="CONTENT"),
    ]
    discovery = [
        {"source_id": 1, "status": "success", "discovered_urls": 1, "usable_urls": 1},
        {"source_id": 2, "status": "success", "discovered_urls": 1, "usable_urls": 1},
    ]
    analysis, opportunities, _ = build_sitemap_analysis(rows, [client, competitor], discovery)
    assert analysis["comparison_status"]["ready"] is True
    assert analysis["comparison_status"]["successful_competitors"] == 1
    assert any(x["gap_type"] == "TOPIC_GAP" and x["topic"] == "Term Insurance" for x in opportunities)


def test_informational_guide_is_content_without_blog_path_or_schema():
    result = classify_page({
        "final_url": "https://example.com/insurance-library/income-tax/what-is-section-80c.html",
        "requested_url": "https://example.com/insurance-library/income-tax/what-is-section-80c.html",
        "title": "What is Section 80C of the Income Tax Act?",
        "meta_description": "A complete guide to Section 80C deductions and eligibility.",
        "h1": ["What is Section 80C?"],
        "h2": ["How Section 80C works", "Eligible investments", "Things to know"],
        "schema_types": [],
        "breadcrumbs": ["Insurance Library", "Income Tax"],
        "author_present": False,
        "published_at_text": "",
        "article_element": False,
        "forms_count": 0,
        "cta_texts": [],
        "main_internal_links_count": 4,
        "word_count": 1200,
        "text_excerpt": "Learn how Section 80C works, what deductions are available, and important things to know before filing income tax.",
        "error": None,
    })
    assert result["detailed_page_type"] == "CONTENT"
    assert result["broad_page_type"] == "CONTENT"
