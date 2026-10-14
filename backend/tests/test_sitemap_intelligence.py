from types import SimpleNamespace

from app.services.sitemap_classifier import classify_page
from app.services.sitemap_fetcher import normalize_url
from app.services.sitemap_topics import classify_topic
from app.services.sitemap_analysis import build_sitemap_analysis


def ev(**kw):
    base = {
        "final_url": "https://example.com/x", "requested_url": "https://example.com/x",
        "title": "", "meta_description": "", "h1": [], "h2": [], "schema_types": [],
        "breadcrumbs": [], "author_present": False, "published_at_text": "", "article_element": False,
        "forms_count": 0, "cta_texts": [], "word_count": 500, "text_excerpt": "", "error": None,
    }
    base.update(kw); return base


def test_normalize_url_removes_fragment_and_trailing_slash():
    assert normalize_url("https://Example.com/a/b/#x") == "https://example.com/a/b"


def test_product_classification_uses_content_signals():
    result = classify_page(ev(
        final_url="https://example.com/money/borrow-now",
        title="Personal Loan - Apply Online",
        h1=["Personal Loan"],
        h2=["Eligibility", "Interest Rates", "Documents Required", "Fees and Charges"],
        schema_types=["FinancialProduct"],
        cta_texts=["Apply Now", "Check Eligibility"],
        forms_count=1,
    ))
    assert result["detailed_page_type"] == "PRODUCT"
    assert result["confidence"] >= 0.9


def test_content_classification_uses_article_evidence_not_url_only():
    result = classify_page(ev(
        final_url="https://example.com/credit-score-improvement",
        title="10 Ways to Improve Your Credit Score",
        h1=["How to Improve Your Credit Score"],
        schema_types=["BlogPosting"],
        author_present=True,
        published_at_text="2026-08-01",
        article_element=True,
        word_count=1500,
    ))
    assert result["detailed_page_type"] == "CONTENT"
    assert result["confidence"] >= 0.9


def test_topic_classification_uses_page_content():
    result = classify_topic(ev(
        final_url="https://example.com/guides/a1",
        title="How to improve your CIBIL score",
        h1=["Credit Score Improvement Guide"],
        text_excerpt="Credit score and CIBIL score tips for borrowers",
    ))
    assert result["category"] == "Credit & Cards"
    assert result["topic"] == "Credit Score"


def test_gap_analysis_detects_topic_gap():
    client = SimpleNamespace(id=1, role="CLIENT", label="Client", root_url="https://client/sitemap.xml")
    c1 = SimpleNamespace(id=2, role="COMPETITOR", label="C1", root_url="https://c1/sitemap.xml")
    c2 = SimpleNamespace(id=3, role="COMPETITOR", label="C2", root_url="https://c2/sitemap.xml")
    rows = [
        SimpleNamespace(id=1, source_id=1, error=None, broad_page_type="PRODUCT", detailed_page_type="PRODUCT", category="Credit & Cards", topic="Credit Card", final_url="https://client/credit-card", original_url="https://client/credit-card", title="Credit Card", h1_json=["Credit Card"], word_count=500, root_sitemap=client.root_url, child_sitemap=client.root_url, normalized_url="https://client/credit-card", canonical_url=None, http_status=200, type_confidence=.95, classification_method="deterministic", topic_confidence=.9, topic_method="deterministic", meta_description=None, h2_json=[], schema_types_json=[], breadcrumbs_json=[], author_present=False, published_at_text=None, cta_json=[], lastmod=None, fetch_method="http", retry_count=0, content_hash=None),
        SimpleNamespace(id=2, source_id=2, error=None, broad_page_type="CONTENT", detailed_page_type="CONTENT", category="Credit & Cards", topic="Credit Score", final_url="https://c1/credit", original_url="https://c1/credit", title="Credit Score", h1_json=["Credit Score"], word_count=800),
        SimpleNamespace(id=3, source_id=3, error=None, broad_page_type="CONTENT", detailed_page_type="CONTENT", category="Credit & Cards", topic="Credit Score", final_url="https://c2/credit", original_url="https://c2/credit", title="Credit Score", h1_json=["Credit Score"], word_count=900),
    ]
    analysis, opps, evidence = build_sitemap_analysis(rows, [client, c1, c2])
    assert any(x["gap_type"] == "TOPIC_GAP" and x["topic"] == "Credit Score" for x in opps)
    assert analysis["topic_coverage_matrix"]
    assert evidence
