from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings
from app.services.google_auth import build_genai_client
from app.services.compliance_guard import (
    UNTRUSTED_WEB_DATA_POLICY,
    sanitize_payload_for_ai,
)

settings = get_settings()
logger = logging.getLogger(__name__)

# The configured AI model has two jobs:
# 1) review/refine an existing deterministic opportunity IN PLACE;
# 2) perform deep section-by-section reasoning and surface genuinely NEW gaps.
#
# Structured output keeps the result machine-valid and auditable.
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "opportunity_reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "opportunity_key": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "coverage_assessment": {
                        "type": "string",
                        "enum": ["covered", "partial", "missing", "uncertain"],
                    },
                    "intent_relevance": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "uncertain"],
                    },
                    "missing_aspects": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "recommended_action": {
                        "type": "string",
                        "enum": [
                            "no_change",
                            "expand_existing",
                            "add_new_section",
                            "add_faq",
                            "improve_heading_structure",
                            "manual_review",
                        ],
                    },
                    "recommendation": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "opportunity_key",
                    "confidence",
                    "coverage_assessment",
                    "intent_relevance",
                    "missing_aspects",
                    "recommended_action",
                    "recommendation",
                    "evidence_ids",
                ],
            },
        },
        "section_reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic_id": {"type": "integer"},
                    "same_topic": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "coverage_assessment": {
                        "type": "string",
                        "enum": ["covered", "partial", "missing", "uncertain"],
                    },
                    "intent_relevance": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "uncertain"],
                    },
                    "missing_aspects": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "recommended_action": {
                        "type": "string",
                        "enum": [
                            "no_change",
                            "expand_existing",
                            "add_new_section",
                            "add_faq",
                            "improve_heading_structure",
                            "manual_review",
                        ],
                    },
                    "recommendation": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "topic_id",
                    "same_topic",
                    "confidence",
                    "coverage_assessment",
                    "intent_relevance",
                    "missing_aspects",
                    "recommended_action",
                    "recommendation",
                    "evidence_ids",
                ],
            },
        },
        "new_opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "title": {"type": "string"},
                    "coverage_assessment": {
                        "type": "string",
                        "enum": ["covered", "partial", "missing", "uncertain"],
                    },
                    "intent_relevance": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "uncertain"],
                    },
                    "missing_aspects": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "recommended_action": {
                        "type": "string",
                        "enum": [
                            "no_change",
                            "expand_existing",
                            "add_new_section",
                            "add_faq",
                            "improve_heading_structure",
                            "manual_review",
                        ],
                    },
                    "recommendation": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "category",
                    "priority",
                    "confidence",
                    "title",
                    "coverage_assessment",
                    "intent_relevance",
                    "missing_aspects",
                    "recommended_action",
                    "recommendation",
                    "evidence_ids",
                ],
            },
        },
    },
    "required": ["opportunity_reviews", "section_reviews", "new_opportunities"],
}


def _validated_ids(ids: list[str] | None, valid_evidence: set[str]) -> list[str]:
    return [str(x) for x in (ids or []) if str(x) in valid_evidence]


def _compact_json(payload: dict[str, Any]) -> str:
    """Serialize the complete granular payload without pretty-print overhead."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def refine_with_ai(evidence_payload: dict) -> dict[str, list[dict]]:
    """Deep AI-model analysis over deterministic + granular Client/P0/P1 evidence.

    Important design rules:
    - The AI model never calculates density/word counts/DOM facts.
    - The AI model reviews existing opportunities by opportunity_key so they can be
      UPDATED instead of duplicated in the database.
    - The AI model may create genuinely new aspect/depth/intent opportunities only
      when it cites valid supplied evidence IDs.
    - On any model/auth/quota failure, deterministic analysis remains intact.
    """
    empty = {
        "opportunity_reviews": [],
        "section_reviews": [],
        "new_opportunities": [],
    }

    mode = settings.ai_mode.strip().lower()
    if mode == "off":
        return empty

    deterministic = evidence_payload.get("deterministic_opportunities") or []
    valid_evidence = set((evidence_payload.get("evidence_index") or {}).keys())
    safe_evidence_payload = sanitize_payload_for_ai(evidence_payload)

    if mode == "mock":
        reviews = []
        if deterministic:
            first = deterministic[0]
            reviews.append(
                {
                    "opportunity_key": first.get("opportunity_key", ""),
                    "confidence": min(0.95, float(first.get("confidence", 0.8))),
                    "coverage_assessment": "partial",
                    "intent_relevance": "medium",
                    "missing_aspects": [],
                    "recommended_action": "manual_review",
                    "recommendation": (
                        "Mock deep-review mode confirmed the deterministic finding. "
                        "Use AI_MODE=vertex for configured AI-model analysis."
                    ),
                    "evidence_ids": _validated_ids(
                        first.get("evidence_ids", []), valid_evidence
                    ),
                }
            )
        return {**empty, "opportunity_reviews": reviews}

    if mode not in {"vertex", "api_key"}:
        return empty

    try:
        from google.genai import types

        client = build_genai_client()

        prompt = (
            "You are the deep reasoning layer of an enterprise SEO analysis system.\n"
            f"Prompt version: {settings.ai_prompt_version}; schema version: {settings.ai_schema_version}.\n"
            f"{UNTRUSTED_WEB_DATA_POLICY}\n\n"
            "The payload contains deterministic facts plus FULL GRANULAR analysis:\n"
            "- deterministic opportunities with stable opportunity_key values;\n"
            "- Section Comparison;\n"
            "- Keyword Placement;\n"
            "- FAQ Comparison;\n"
            "- full Client/P0/P1 section text with evidence IDs;\n"
            "- Client/P0/P1 FAQ text;\n"
            "- density/page evidence.\n\n"
            "Your job is to reason section-by-section, especially about content DEPTH and "
            "missing ASPECTS that simple heading similarity/word counts cannot detect. "
            "Compare Client with P0 and P1. Determine whether the same topic is actually "
            "covered, partially covered, missing, or uncertain; identify concrete missing "
            "aspects; judge whether the benchmark topic is relevant to the client page's "
            "apparent intent; and recommend expand/add/no-change/manual-review actions.\n\n"
            "STRICT RULES:\n"
            "1. Never invent rankings, traffic, conversions, search volume, business impact, "
            "or facts not supplied.\n"
            "2. Never treat competitor presence alone as proof that Client must add content.\n"
            "3. Do not copy competitor wording. Recommend original topic/aspect coverage only.\n"
            "4. Every review/recommendation MUST cite one or more valid evidence_ids.\n"
            "5. Existing deterministic opportunities MUST be returned in opportunity_reviews "
            "using the SAME opportunity_key. Do not recreate them as new_opportunities.\n"
            "6. new_opportunities are allowed only for genuinely new aspect/depth/intent gaps "
            "not represented by an existing deterministic opportunity.\n"
            "7. Prefer expanding an existing Client section when the topic is already present; "
            "avoid duplicate sections.\n"
            "8. Treat benchmark word counts as evidence, never mandatory targets.\n"
            "9. Keep recommendations specific, concise, and implementation-oriented.\n"
            "10. Editorial priorities are HIGH, MEDIUM, LOW. P0/P1 are URL benchmark roles only.\n"
            "11. Never use benchmark labels as editorial priority labels.\n\n"
            "DATA:\n"
            + _compact_json(safe_evidence_payload)
        )

        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(
                    thinking_level=settings.ai_thinking_level_deep
                ),
                response_mime_type="application/json",
                response_schema=AI_SCHEMA,
            ),
        )

        data = sanitize_payload_for_ai(json.loads(response.text or "{}"))
        valid_keys = {
            str(x.get("opportunity_key"))
            for x in deterministic
            if x.get("opportunity_key")
        }
        valid_topic_ids = {
            int(x.get("Topic ID"))
            for x in (evidence_payload.get("granular_analysis", {}).get("section_comparison") or [])
            if x.get("Topic ID") is not None
        }

        reviews = []
        for item in data.get("opportunity_reviews", []):
            key = str(item.get("opportunity_key") or "")
            ids = _validated_ids(item.get("evidence_ids"), valid_evidence)
            if key not in valid_keys or not ids:
                continue
            item["opportunity_key"] = key
            item["evidence_ids"] = ids
            reviews.append(item)

        section_reviews = []
        for item in data.get("section_reviews", []):
            try:
                topic_id = int(item.get("topic_id"))
            except (TypeError, ValueError):
                continue
            ids = _validated_ids(item.get("evidence_ids"), valid_evidence)
            if topic_id not in valid_topic_ids or not ids:
                continue
            item["topic_id"] = topic_id
            item["evidence_ids"] = ids
            section_reviews.append(item)

        new_items = []
        for item in data.get("new_opportunities", []):
            ids = _validated_ids(item.get("evidence_ids"), valid_evidence)
            if not ids:
                continue
            item["evidence_ids"] = ids
            new_items.append(item)

        return {
            "opportunity_reviews": reviews[: settings.ai_max_opportunities],
            "section_reviews": section_reviews[: max(settings.ai_max_opportunities * 3, 30)],
            "new_opportunities": new_items[: settings.ai_max_opportunities],
        }

    except Exception as exc:
        logger.warning(
            "Deep AI analysis unavailable; deterministic results retained: %s", exc
        )
        return empty
