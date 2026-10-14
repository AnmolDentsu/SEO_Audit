from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.services.google_auth import build_genai_client
from app.services.compliance_guard import (
    UNTRUSTED_WEB_DATA_POLICY,
    sanitize_payload_for_ai,
)

settings = get_settings()
logger = logging.getLogger(__name__)

PAGE_TYPES = [
    "HOMEPAGE",
    "PRODUCT",
    "CONTENT",
    "CATEGORY",
    "TOOL_CALCULATOR",
    "FAQ_HELP",
    "LANDING_PAGE",
    "UTILITY",
    "OTHER",
]

PAGE_CLASS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "page_type": {"type": "string", "enum": PAGE_TYPES},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason_codes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["item_id", "page_type", "confidence", "reason_codes"],
            },
        }
    },
    "required": ["items"],
}

TOPIC_CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cluster_id": {"type": "string"},
                    "category": {"type": "string"},
                    "topic": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["cluster_id", "category", "topic", "confidence"],
            },
        }
    },
    "required": ["clusters"],
}

OPPORTUNITY_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "opportunity_key": {"type": "string"},
                    "decision": {"type": "string", "enum": ["KEEP", "REVIEW", "NO_CHANGE"]},
                    "business_relevance": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "uncertain"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "recommended_page_type": {
                        "type": "string",
                        "enum": [
                            "PRODUCT",
                            "CONTENT",
                            "CATEGORY",
                            "TOOL_CALCULATOR",
                            "FAQ_HELP",
                            "OTHER",
                            "NO_CHANGE",
                        ],
                    },
                    "recommendation": {"type": "string"},
                    "supporting_subtopics": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "opportunity_key",
                    "decision",
                    "business_relevance",
                    "confidence",
                    "recommended_page_type",
                    "recommendation",
                    "supporting_subtopics",
                    "evidence_ids",
                ],
            },
        }
    },
    "required": ["reviews"],
}


def _page_payload(item: dict) -> dict:
    return {
        "item_id": item["item_id"],
        "url": item.get("final_url") or item.get("requested_url"),
        "title": item.get("title", ""),
        "meta_description": item.get("meta_description", ""),
        "h1": item.get("h1", []),
        "h2": (item.get("h2") or [])[:20],
        "schema_types": item.get("schema_types", []),
        "breadcrumbs": item.get("breadcrumbs", []),
        "author_present": bool(item.get("author_present")),
        "published_at_present": bool(item.get("published_at_text")),
        "cta_texts": (item.get("cta_texts") or [])[:12],
        "forms_count": item.get("forms_count", 0),
        "main_internal_links_count": item.get("main_internal_links_count", 0),
        "word_count": item.get("word_count", 0),
        "text_excerpt": (item.get("text_excerpt") or "")[:3000],
        "deterministic_classification": item.get("classification", {}),
    }


def classify_ambiguous_pages_ai(items: list[dict]) -> dict[int, dict]:
    if not items or settings.ai_mode.strip().lower() in {"off", "mock"}:
        return {}

    client = build_genai_client()
    from google.genai import types

    output: dict[int, dict] = {}
    for start in range(0, len(items), settings.sitemap_ai_page_batch_size):
        batch = items[start : start + settings.sitemap_ai_page_batch_size]
        safe_batch = sanitize_payload_for_ai([_page_payload(x) for x in batch])
        prompt = (
            f"Prompt version={settings.ai_prompt_version}; schema version={settings.ai_schema_version}. "
            f"{UNTRUSTED_WEB_DATA_POLICY}\n\n"
            "Classify resolved webpages for an enterprise SEO sitemap inventory. "
            "Use only supplied evidence. "
            "The logic must be industry-agnostic: the pages can belong to banking, airlines, healthcare, retail, travel, software, automotive, education or any other sector. "
            "URL path is only a weak signal; prioritize structured data, title/H1/H2, editorial metadata, main-content transaction CTAs/forms, link-rich hub structure and actual page content. "
            "Return one of HOMEPAGE, PRODUCT, CONTENT, CATEGORY, TOOL_CALCULATOR, FAQ_HELP, LANDING_PAGE, UTILITY, OTHER. "
            "PRODUCT includes a product or service detail/conversion page; CONTENT includes blogs, articles, guides, educational/explainer pages, news/editorial resources and other informational content. "
            "Do not infer rankings, traffic, or business value. JSON data follows:\n"
            + json.dumps(safe_batch, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_level=settings.ai_thinking_level_fast
                    ),
                    response_mime_type="application/json",
                    response_schema=PAGE_CLASS_SCHEMA,
                ),
            )
            data = sanitize_payload_for_ai(json.loads(response.text or "{}"))
            valid_ids = {int(x["item_id"]) for x in batch}
            for row in data.get("items", []):
                try:
                    item_id = int(row.get("item_id"))
                except Exception:
                    continue
                if item_id in valid_ids and row.get("page_type") in PAGE_TYPES:
                    output[item_id] = row
        except Exception as exc:
            logger.warning(
                "AI-model page classification batch failed; deterministic result retained: %s",
                exc,
            )
    return output


def label_topic_clusters_ai(clusters: list[dict]) -> dict[str, dict]:
    """Assign canonical category/topic names to semantic clusters.

    The labels are derived from cluster variants and real page evidence and do not
    rely on a fixed industry taxonomy.
    """
    if not clusters or settings.ai_mode.strip().lower() in {"off", "mock"}:
        return {}

    client = build_genai_client()
    from google.genai import types

    output: dict[str, dict] = {}
    for start in range(0, len(clusters), settings.sitemap_ai_topic_cluster_batch_size):
        batch = clusters[start : start + settings.sitemap_ai_topic_cluster_batch_size]
        safe_batch = sanitize_payload_for_ai(batch)
        prompt = (
            f"Prompt version={settings.ai_prompt_version}; schema version={settings.ai_schema_version}. "
            f"{UNTRUSTED_WEB_DATA_POLICY}\n\n"
            "Name semantic topic clusters for a cross-site enterprise sitemap analysis. "
            "Do not assume an industry. Derive a concise broad category and a concise canonical topic strictly from the cluster variants and sample pages. "
            "Keep labels descriptive, neutral, reusable across competing websites and free of company/brand names when the underlying concept is generic. "
            "Do not invent products, demand, rankings, traffic or business value.\nDATA:\n"
            + json.dumps(safe_batch, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_level=settings.ai_thinking_level_fast
                    ),
                    response_mime_type="application/json",
                    response_schema=TOPIC_CLUSTER_SCHEMA,
                ),
            )
            data = sanitize_payload_for_ai(json.loads(response.text or "{}"))
            valid = {str(x.get("cluster_id")) for x in batch}
            for row in data.get("clusters", []):
                cid = str(row.get("cluster_id") or "")
                category = str(row.get("category") or "").strip()
                topic = str(row.get("topic") or "").strip()
                if cid in valid and category and topic:
                    output[cid] = {
                        "category": category[:120],
                        "topic": topic[:180],
                        "confidence": float(row.get("confidence") or 0),
                    }
        except Exception as exc:
            logger.warning("AI-model topic-cluster labeling failed; fallback labels retained: %s", exc)
    return output


def review_sitemap_opportunities_ai(
    opportunities: list[dict],
    evidence_index: dict[str, dict],
) -> dict[str, dict]:
    if not opportunities or settings.ai_mode.strip().lower() in {"off", "mock"}:
        return {}

    candidates = [
        x for x in opportunities
        if str(x.get("priority") or "").upper() in {"HIGH", "MEDIUM"}
    ][: settings.sitemap_ai_max_opportunities]
    if not candidates:
        return {}
    compact_evidence = {
        k: v
        for k, v in evidence_index.items()
        if any(k in (o.get("evidence_ids") or []) for o in candidates)
    }
    safe_review_payload = sanitize_payload_for_ai(
        {
            "opportunities": candidates,
            "evidence_index": compact_evidence,
        }
    )
    prompt = (
        f"Prompt version={settings.ai_prompt_version}; schema version={settings.ai_schema_version}. "
        f"{UNTRUSTED_WEB_DATA_POLICY}\n\n"
        "You are the reasoning layer for sitemap-based SEO content-gap analysis across arbitrary industries. "
        "Review deterministic gaps using only supplied evidence. "
        "Competitor presence alone is not proof that the client should copy a topic. Never invent search demand, rankings, traffic, revenue, product availability, or business impact. "
        "Pages marked utility, error, security challenge, access denied or taxonomy-ineligible are not valid content-gap evidence. "
        "Assess business relevance only from the client's supplied related coverage; if unclear say uncertain. Recommend an appropriate page type or NO_CHANGE. "
        "Keep language original and evidence-grounded. Every review must cite valid supplied evidence IDs.\nDATA:\n"
        + json.dumps(
            safe_review_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    try:
        from google.genai import types

        client = build_genai_client()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(
                    thinking_level=settings.ai_thinking_level_deep
                ),
                response_mime_type="application/json",
                response_schema=OPPORTUNITY_SCHEMA,
            ),
        )
        data = sanitize_payload_for_ai(json.loads(response.text or "{}"))
    except Exception as exc:
        logger.warning(
            "AI-model sitemap opportunity review failed; deterministic results retained: %s",
            exc,
        )
        return {}

    valid_keys = {str(x.get("opportunity_key")) for x in candidates}
    valid_evidence = set(evidence_index)
    result: dict[str, dict] = {}
    for row in data.get("reviews", []):
        key = str(row.get("opportunity_key") or "")
        ids = [str(x) for x in row.get("evidence_ids", []) if str(x) in valid_evidence]
        if key in valid_keys and ids:
            row["evidence_ids"] = ids
            result[key] = row
    return result
