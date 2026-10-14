from __future__ import annotations

import re
from urllib.parse import urlparse

from app.config import get_settings

settings = get_settings()

# Detailed page types. CONTENT deliberately replaces the narrower BLOG bucket:
# many enterprise sites publish guides/articles/education pages without BlogPosting
# schema, author/date metadata, or a /blog/ path.
PAGE_TYPES = {
    "HOMEPAGE",
    "PRODUCT",
    "CONTENT",
    "CATEGORY",
    "TOOL_CALCULATOR",
    "FAQ_HELP",
    "LANDING_PAGE",
    "UTILITY",
    "OTHER",
}

PRODUCT_SCHEMA = {
    "Product",
    "Service",
    "Offer",
    "AggregateOffer",
    "FinancialProduct",
    "BankAccount",
    "LoanOrCredit",
    "SoftwareApplication",
    "WebApplication",
}
ARTICLE_SCHEMA = {
    "Article",
    "BlogPosting",
    "NewsArticle",
    "ScholarlyArticle",
    "TechArticle",
    "Report",
}
CATEGORY_SCHEMA = {"CollectionPage", "ItemList"}
FAQ_SCHEMA = {"FAQPage", "QAPage"}

# Generic commercial/service intent. These signals are evaluated only from the
# extracted main-content evidence, so global header/footer CTAs do not dominate.
COMMERCIAL_HEADING_TERMS = re.compile(
    r"\b(pricing|price|plans?|packages?|rates?|fares?|fees?|charges?|specifications?|"
    r"features?|eligibility|requirements?|product details?|service details?|what'?s included|"
    r"compare plans?|availability|book(?:ing)?|subscription|membership|apply|buy|purchase|"
    r"quote|request demo|schedule appointment|open account|start trial)\b",
    re.I,
)

# Broad informational/editorial intent. This intentionally covers articles,
# guides, explainers, educational pages and editorial resources -- not just blogs.
CONTENT_TERMS = re.compile(
    r"\b(reading time|published|updated on|author|guide|tips|ways to|how to|what is|"
    r"what are|why |when |where |who |explained|meaning|benefits of|advantages of|"
    r"disadvantages of|complete guide|step by step|everything you need to know|"
    r"things to know|learn about|understanding|overview of|difference between|"
    r"comparison|examples?|checklist|faq|frequently asked questions)\b",
    re.I,
)

QUESTIONISH_TITLE = re.compile(
    r"^(what|why|how|when|where|who|which|can|should|is|are|do|does)\b",
    re.I,
)

UTILITY_TERMS = re.compile(
    r"\b(privacy|terms|disclaimer|contact us|about us|careers?|jobs?|login|sign in|sitemap|"
    r"cookie policy|grievance|branch locator|store locator|office locator|investor relations|"
    r"press release|media centre|media center|board of directors|corporate governance)\b",
    re.I,
)
TOOL_TERMS = re.compile(
    r"\b(calculator|calculate|checker|estimator|converter|simulator|planner|tracker|"
    r"comparison tool|assessment tool)\b",
    re.I,
)
FAQ_TERMS = re.compile(r"\b(faq|frequently asked questions|help centre|help center|support)\b", re.I)


def broad_page_type(detailed: str) -> str:
    # Backward compatibility for cached/historical BLOG classifications.
    if detailed == "BLOG":
        return "CONTENT"
    return detailed if detailed in {"HOMEPAGE", "PRODUCT", "CONTENT"} else "OTHER"


def _score_to_confidence(top: float, second: float) -> float:
    if top <= 0:
        return 0.4
    margin = max(0.0, top - second)
    if top >= 9 and margin >= 3:
        return 0.97
    if top >= 7 and margin >= 2:
        return 0.93
    if top >= 5 and margin >= 1.5:
        return 0.87
    if top >= 3:
        return min(0.82, 0.62 + margin * 0.06 + top * 0.02)
    return min(0.68, 0.48 + margin * 0.06)


def classify_page(evidence: dict) -> dict:
    url = evidence.get("final_url") or evidence.get("requested_url") or ""
    path = urlparse(url).path or "/"
    title = evidence.get("title") or ""
    h1 = " ".join(evidence.get("h1") or [])
    h2 = " ".join(evidence.get("h2") or [])
    meta = evidence.get("meta_description") or ""
    text_excerpt = (evidence.get("text_excerpt") or "")[:5000]
    schemas = set(evidence.get("schema_types") or [])
    ctas = " ".join(evidence.get("cta_texts") or [])

    scores = {k: 0.0 for k in PAGE_TYPES}
    reasons: dict[str, list[str]] = {k: [] for k in PAGE_TYPES}

    if path in {"", "/"}:
        return {
            "detailed_page_type": "HOMEPAGE",
            "broad_page_type": "HOMEPAGE",
            "confidence": 0.995,
            "method": "deterministic",
            "evidence": ["Resolved URL is the domain root"],
            "scores": {"HOMEPAGE": 10.0},
        }

    def add(kind: str, points: float, reason: str):
        scores[kind] += points
        reasons[kind].append(reason)

    product_schema = schemas & PRODUCT_SCHEMA
    article_schema = schemas & ARTICLE_SCHEMA
    category_schema = schemas & CATEGORY_SCHEMA
    faq_schema = schemas & FAQ_SCHEMA

    if product_schema:
        add("PRODUCT", 5.0, f"Product/service schema: {', '.join(sorted(product_schema))}")
    if article_schema:
        add("CONTENT", 5.0, f"Editorial/article schema: {', '.join(sorted(article_schema))}")
    if category_schema:
        add("CATEGORY", 4.0, f"Collection/list schema: {', '.join(sorted(category_schema))}")
    if faq_schema:
        add("FAQ_HELP", 5.0, "FAQ/Q&A structured data")
        add("CONTENT", 1.5, "FAQ content is informational")

    if evidence.get("author_present"):
        add("CONTENT", 2.5, "Author signal present")
    if evidence.get("published_at_text"):
        add("CONTENT", 2.5, "Published/updated date signal present")
    if evidence.get("article_element"):
        add("CONTENT", 1.8, "<article> element present")

    commercial_text = " ".join([title, h1, h2, ctas])
    commercial_hits = len(COMMERCIAL_HEADING_TERMS.findall(commercial_text))
    if commercial_hits:
        add(
            "PRODUCT",
            min(3.0, 0.6 + commercial_hits * 0.55),
            f"{commercial_hits} commercial/service intent signal(s)",
        )
    if evidence.get("cta_texts"):
        add("PRODUCT", 2.2, "Transaction/conversion CTA detected in main content")
    if evidence.get("forms_count", 0) > 0 and (commercial_hits or evidence.get("cta_texts")):
        add("PRODUCT", 1.5, "Main-content form present with commercial/service intent")

    editorial_probe = " ".join([title, h1, h2, meta, text_excerpt[:1500]])
    content_hits = len(CONTENT_TERMS.findall(editorial_probe))
    if content_hits:
        add("CONTENT", min(3.0, 0.7 + content_hits * 0.45), f"{content_hits} informational/editorial wording signal(s)")
    if QUESTIONISH_TITLE.search((title or h1).strip()):
        add("CONTENT", 1.5, "Question/explainer title intent")
    if evidence.get("word_count", 0) >= 700:
        # Long-form content alone is weak evidence, but it helps resolve product
        # directories that actually contain educational guides/articles.
        add("CONTENT", 0.7, "Long-form main content")
    if evidence.get("word_count", 0) >= 1200 and (content_hits or article_schema):
        add("CONTENT", 1.0, "Long-form content with informational/editorial signals")

    if TOOL_TERMS.search(" ".join([title, h1, path])):
        add("TOOL_CALCULATOR", 5.0, "Interactive tool/calculator intent detected")
    if FAQ_TERMS.search(" ".join([title, h1, path])):
        add("FAQ_HELP", 3.0, "FAQ/help intent detected")
    if UTILITY_TERMS.search(" ".join([title, h1, path])):
        add("UTILITY", 5.0, "Corporate/utility intent detected")

    depth = len([x for x in path.split("/") if x])
    internal_links = int(evidence.get("main_internal_links_count") or 0)
    h2_count = len(evidence.get("h2") or [])
    if internal_links >= 20 and h2_count >= 4 and not article_schema and not evidence.get("published_at_text"):
        add("CATEGORY", 2.0, "Hub-like main content with many internal links")
    if depth <= 2 and internal_links >= 30 and evidence.get("word_count", 0) < 3500:
        add("CATEGORY", 1.3, "Shallow, link-rich hub structure")

    # URL hints stay intentionally weak. They may support extracted evidence but
    # must not determine the class on their own.
    low_path = path.casefold()
    if re.search(r"/(blog|blogs|article|articles|news|insights|learn|resources?|library|guides?)/", low_path):
        add("CONTENT", 0.8, "URL-path informational/editorial hint")
    if re.search(r"/(product|products|service|services|plans|solutions)/", low_path):
        add("PRODUCT", 0.6, "URL-path product/service hint")
    if re.search(r"/(category|categories|collections?|topics?)/", low_path):
        add("CATEGORY", 0.7, "URL-path category/hub hint")
    if re.search(r"/(campaign|offer|offers|promo|promotion)/", low_path):
        add("LANDING_PAGE", 1.2, "URL-path landing-page hint")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_kind, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if top_score < 1.0:
        top_kind = "OTHER"
        top_score = 1.0
        second_score = 0.5
        reasons["OTHER"].append("No decisive homepage/product/content/category/tool/utility signal")

    confidence = _score_to_confidence(top_score, second_score)
    return {
        "detailed_page_type": top_kind,
        "broad_page_type": broad_page_type(top_kind),
        "confidence": round(confidence, 3),
        "method": "deterministic",
        "evidence": reasons[top_kind][:8],
        "scores": {k: round(v, 2) for k, v in ranked if v > 0},
    }


def needs_browser_fallback(evidence: dict, classification: dict) -> bool:
    if evidence.get("non_html"):
        return False

    if evidence.get("error"):
        status = evidence.get("status_code")
        # 404/410 are verified once in Chromium when configured. This catches
        # unusual CDN/WAF behaviour, but if the browser also sees 404/410 the URL
        # is treated as a stale sitemap entry and is not retried forever.
        if status in {404, 410}:
            return bool(settings.sitemap_verify_stale_with_browser)
        return status in {None, 401, 403, 406, 408, 425, 429, 500, 502, 503, 504}

    sparse = (
        not evidence.get("title")
        or not evidence.get("h1")
        or evidence.get("word_count", 0) < 80
    )
    return bool(
        sparse
        or classification.get("confidence", 0) < settings.sitemap_browser_fallback_threshold
    )


def needs_ai_review(classification: dict) -> bool:
    return classification.get("confidence", 0) < 0.70
