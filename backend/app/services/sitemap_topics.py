from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import unquote, urlparse

from app import models
from app.config import get_settings

settings = get_settings()

_GENERIC_PREFIXES = re.compile(
    r"^(what is|what are|how to|how does|how do|why is|why are|guide to|complete guide to|"
    r"everything you need to know about|benefits of|advantages of|types of|understanding)\s+",
    re.I,
)
_GENERIC_SUFFIXES = re.compile(
    r"\s+(?:guide|overview|explained|online|page|details?)$",
    re.I,
)
_GENERIC_PATH_SEGMENTS = {
    "blog",
    "blogs",
    "article",
    "articles",
    "news",
    "insights",
    "learn",
    "resources",
    "resource",
    "products",
    "product",
    "services",
    "service",
    "category",
    "categories",
    "topics",
    "topic",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _display_slug(value: str) -> str:
    value = unquote(value or "").replace("-", " ").replace("_", " ")
    value = _clean(value)
    return value.title() if value else ""


def candidate_topic_label(evidence: dict) -> str:
    h1 = next(iter(evidence.get("h1") or []), "")
    title = evidence.get("title") or ""
    value = _clean(h1 or title)
    # Remove common brand/site suffixes introduced after a separator without
    # assuming the brand name or industry.
    value = re.sub(r"\s+[|–—]\s+.*$", "", value).strip()
    if not value:
        path = urlparse(evidence.get("final_url") or evidence.get("requested_url") or "").path
        segment = next((x for x in reversed(path.split("/")) if x), "")
        value = _display_slug(segment)
    value = _GENERIC_PREFIXES.sub("", value)
    value = _GENERIC_SUFFIXES.sub("", value)
    return _clean(value)[:180] or "Other / Unclassified"


def candidate_category_label(evidence: dict) -> str:
    breadcrumbs = [_clean(x) for x in (evidence.get("breadcrumbs") or []) if _clean(x)]
    # Breadcrumb extraction usually contains ancestor links only, so the final
    # meaningful crumb is a strong generic parent/category signal.
    if breadcrumbs:
        for value in reversed(breadcrumbs):
            low = value.casefold()
            if low not in {"home", "homepage"} and len(value) >= 2:
                return value[:120]

    path = urlparse(evidence.get("final_url") or evidence.get("requested_url") or "").path
    segments = [x for x in path.split("/") if x]
    for segment in segments:
        low = segment.casefold()
        if low not in _GENERIC_PATH_SEGMENTS:
            label = _display_slug(segment)
            if label:
                return label[:120]
    return "General"


def _normalize_topic_key(text: str) -> str:
    """Aggressive-but-auditable normalization for very large sitemap taxonomies.

    Numeric/model/size variants are retained later as Subtopics/Variants while the
    canonical key groups pages that express the same underlying content family.
    """
    value = _GENERIC_PREFIXES.sub("", _clean(text).casefold())
    value = _GENERIC_SUFFIXES.sub("", value)
    # Normalize years, score values, model numbers, dimensions and percentages.
    value = re.sub(r"\b(?:19|20)\d{2}\b", " {year} ", value)
    value = re.sub(r"\b\d+(?:\.\d+)?(?:%|cm|mm|inch|inches|kg|gb|tb|cc|mah)?\b", " {n} ", value)
    value = re.sub(r"\b(?:x|×)\s*{n}\b", " {n} ", value)
    value = re.sub(r"[^\w{}\s]", " ", value)
    value = re.sub(r"(?:\s*{n}\s*){2,}", " {n} ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "other unclassified"


def preliminary_topic(evidence: dict) -> dict:
    if evidence.get("error"):
        return {
            "category": "Other / Unclassified",
            "topic": "Other / Unclassified",
            "subtopic": "",
            "confidence": 0.0,
            "method": "fetch-error",
        }
    label = candidate_topic_label(evidence)
    if label == "Other / Unclassified":
        return {
            "category": candidate_category_label(evidence),
            "topic": label,
            "subtopic": "",
            "confidence": 0.3,
            "method": "content-candidate",
        }
    confidence = 0.68 if (evidence.get("h1") or evidence.get("title")) else 0.5
    return {
        "category": candidate_category_label(evidence),
        "topic": label,
        "subtopic": "",
        "confidence": confidence,
        "method": "content-candidate",
    }


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().casefold().encode()).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class PersistentEmbeddingService:
    def __init__(self, db):
        self.db = db
        self.client = None
        if settings.ai_mode.strip().lower() in {"vertex", "api_key"}:
            try:
                from app.services.google_auth import build_genai_client

                self.client = build_genai_client()
            except Exception:
                self.client = None

    def _cached(self, text: str):
        row = self.db.query(models.SitemapEmbeddingCache).filter_by(
            text_hash=_hash(text),
            model_key=settings.embedding_model,
            dimension=settings.embedding_dimension,
        ).first()
        return row.vector_json if row else None

    def _save_many(self, rows: list[tuple[str, list[float]]]):
        if not rows:
            return
        now = datetime.now(timezone.utc)
        for text, vector in rows:
            if self.db.query(models.SitemapEmbeddingCache).filter_by(
                text_hash=_hash(text),
                model_key=settings.embedding_model,
                dimension=settings.embedding_dimension,
            ).first():
                continue
            self.db.add(models.SitemapEmbeddingCache(
                text_hash=_hash(text),
                text_preview=text[:300],
                model_key=settings.embedding_model,
                dimension=settings.embedding_dimension,
                vector_json=vector,
                created_at=now,
            ))
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()

    def embed_many(self, texts: Iterable[str]) -> dict[str, list[float]]:
        unique = list(dict.fromkeys(x.strip() for x in texts if x and x.strip()))
        result: dict[str, list[float]] = {}
        missing: list[str] = []
        for text in unique:
            vec = self._cached(text)
            if vec:
                result[text] = vec
            else:
                missing.append(text)
        if not missing or not self.client:
            return result

        from google.genai import types

        for start in range(0, len(missing), settings.sitemap_embedding_batch_size):
            batch = missing[start : start + settings.sitemap_embedding_batch_size]
            try:
                config = types.EmbedContentConfig(
                    output_dimensionality=settings.embedding_dimension
                )
                if settings.embedding_model != "gemini-embedding-2":
                    config.task_type = "SEMANTIC_SIMILARITY"
                response = self.client.models.embed_content(
                    model=settings.embedding_model,
                    contents=batch,
                    config=config,
                )
                vectors = [x.values for x in (response.embeddings or [])]
                if len(vectors) != len(batch):
                    raise RuntimeError("Embedding response count mismatch")
                saved: list[tuple[str, list[float]]] = []
                for text, vec in zip(batch, vectors):
                    result[text] = vec
                    saved.append((text, vec))
                self._save_many(saved)
            except Exception:
                # Semantic enhancement must never fail the sitemap run.
                continue
        return result


def _cluster_id(label: str) -> str:
    return "TCL-" + hashlib.sha1(_normalize_topic_key(label).encode()).hexdigest()[:12]


def build_dynamic_topic_taxonomy(db, rows: list[dict], *, enable_ai: bool) -> list[dict]:
    """Create a domain-agnostic topic taxonomy from actual page evidence.

    Exact normalized labels are grouped first. A bounded set of the most common
    labels is then semantically merged using the existing persistent embedding
    cache. Finally, when AI is enabled, high-volume clusters can be given concise
    canonical category/topic labels from their own evidence.

    No BFSI/airline/retail taxonomy is hard-coded here.
    """
    label_rows: dict[str, list[int]] = defaultdict(list)
    display_by_key: dict[str, str] = {}
    category_candidates: dict[str, list[str]] = defaultdict(list)

    for index, row in enumerate(rows):
        if row.get("error"):
            continue
        label = candidate_topic_label(row)
        if label == "Other / Unclassified":
            continue
        key = _normalize_topic_key(label)
        label_rows[key].append(index)
        display_by_key.setdefault(key, label)
        category_candidates[key].append(candidate_category_label(row))

    if not label_rows:
        return []

    frequencies = Counter({k: len(v) for k, v in label_rows.items()})
    ordered_keys = [k for k, _ in frequencies.most_common()]

    # Default: every exact-normalized topic is its own cluster.
    key_to_cluster = {k: _cluster_id(display_by_key[k]) for k in ordered_keys}
    clusters: dict[str, dict] = {}
    semantic_cluster_ids: set[str] = set()
    for key in ordered_keys:
        cid = key_to_cluster[key]
        clusters[cid] = {
            "cluster_id": cid,
            "representative": display_by_key[key],
            "keys": [key],
            "page_count": frequencies[key],
            "variants": [display_by_key[key]],
        }

    # Semantic merging is bounded to the most frequent labels so very large
    # inventories cannot create uncontrolled quadratic work.
    semantic_keys = ordered_keys[: settings.sitemap_semantic_cluster_max_labels]
    if settings.ai_mode.strip().lower() in {"vertex", "api_key"} and semantic_keys:
        service = PersistentEmbeddingService(db)
        texts = [display_by_key[k] for k in semantic_keys]
        vectors = service.embed_many(texts)
        semantic_clusters: list[dict] = []

        for key in semantic_keys:
            label = display_by_key[key]
            vector = vectors.get(label)
            if not vector:
                continue
            best_score = 0.0
            best_cluster = None
            for cluster in semantic_clusters:
                score = _cosine(vector, cluster["vector"])
                if score > best_score:
                    best_score = score
                    best_cluster = cluster
            if best_cluster and best_score >= settings.sitemap_semantic_topic_threshold:
                cid = best_cluster["cluster_id"]
                key_to_cluster[key] = cid
                best_cluster["keys"].append(key)
                best_cluster["page_count"] += frequencies[key]
                best_cluster["variants"].append(label)
            else:
                cid = _cluster_id(label)
                semantic_clusters.append({
                    "cluster_id": cid,
                    "representative": label,
                    "vector": vector,
                    "keys": [key],
                    "page_count": frequencies[key],
                    "variants": [label],
                })
                key_to_cluster[key] = cid

        # Rebuild cluster metadata for semantically processed labels, preserving
        # exact-only clusters outside the bounded semantic set.
        for cluster in semantic_clusters:
            semantic_cluster_ids.add(cluster["cluster_id"])
            clusters[cluster["cluster_id"]] = {
                k: v for k, v in cluster.items() if k != "vector"
            }
        for key in semantic_keys:
            cid = key_to_cluster[key]
            # Remove stale exact singleton cluster if this key merged elsewhere.
            singleton = _cluster_id(display_by_key[key])
            if singleton != cid and singleton in clusters:
                clusters.pop(singleton, None)

    # Consolidate cluster variants/page counts after semantic remapping.
    consolidated: dict[str, dict] = {}
    for key in ordered_keys:
        cid = key_to_cluster[key]
        row = consolidated.setdefault(cid, {
            "cluster_id": cid,
            "representative": display_by_key[key],
            "keys": [],
            "page_count": 0,
            "variants": [],
        })
        row["keys"].append(key)
        row["page_count"] += frequencies[key]
        if display_by_key[key] not in row["variants"]:
            row["variants"].append(display_by_key[key])
    clusters = consolidated

    ai_labels: dict[str, dict] = {}
    if enable_ai and settings.ai_mode.strip().lower() not in {"off", "mock"}:
        from app.services.sitemap_ai import label_topic_clusters_ai

        candidates = []
        for cluster in sorted(clusters.values(), key=lambda x: -x["page_count"])[
            : settings.sitemap_ai_max_topic_clusters
        ]:
            sample_pages = []
            for key in cluster["keys"]:
                for idx in label_rows[key][:3]:
                    r = rows[idx]
                    sample_pages.append({
                        "title": r.get("title") or "",
                        "h1": (r.get("h1") or [])[:2],
                        "breadcrumbs": (r.get("breadcrumbs") or [])[:8],
                        "page_type": r.get("detailed_page_type") or "OTHER",
                    })
                    if len(sample_pages) >= 8:
                        break
                if len(sample_pages) >= 8:
                    break
            candidates.append({
                "cluster_id": cluster["cluster_id"],
                "representative": cluster["representative"],
                "variants": cluster["variants"][:10],
                "page_count": cluster["page_count"],
                "sample_pages": sample_pages,
            })
        ai_labels = label_topic_clusters_ai(candidates)

    cluster_output: list[dict] = []
    for cluster in clusters.values():
        ai = ai_labels.get(cluster["cluster_id"])
        # Generic fallback category comes from the most common actual breadcrumb/
        # path-derived parent among pages in the cluster.
        cats: list[str] = []
        for key in cluster["keys"]:
            cats.extend(category_candidates.get(key, []))
        fallback_category = Counter(cats).most_common(1)[0][0] if cats else "General"
        category = _clean((ai or {}).get("category")) or fallback_category
        topic = _clean((ai or {}).get("topic")) or cluster["representative"]
        confidence = float((ai or {}).get("confidence") or 0.78)
        semantic_used = cluster["cluster_id"] in semantic_cluster_ids
        if ai and semantic_used:
            method = "dynamic-semantic+ai"
        elif ai:
            method = "dynamic-exact+ai"
        elif semantic_used:
            method = "dynamic-semantic"
        else:
            method = "dynamic-exact"

        variant_count = len(cluster["variants"])
        consolidation_reason = (
            "AI-labeled semantic cluster" if ai and semantic_used else
            "AI-labeled normalized cluster" if ai else
            "Embedding similarity merged normalized labels" if semantic_used else
            "Normalized exact family; numeric/year/model variants retained as variants"
        )
        cluster_output.append({
            "cluster_id": cluster["cluster_id"],
            "category": category[:120],
            "topic": topic[:180],
            "confidence": round(min(max(confidence, 0.0), 1.0), 3),
            "confidence_meaning": "taxonomy evidence strength; not SEO success probability",
            "method": method,
            "consolidation_reason": consolidation_reason,
            "page_count": cluster["page_count"],
            "variant_count": variant_count,
            "variants": cluster["variants"][:20],
        })

        for key in cluster["keys"]:
            for index in label_rows[key]:
                rows[index]["category"] = category[:120]
                rows[index]["topic"] = topic[:180]
                rows[index]["topic_confidence"] = round(min(max(confidence, 0.0), 1.0), 3)
                rows[index]["topic_method"] = method
                rows[index]["subtopic"] = display_by_key[key] if display_by_key[key] != topic else ""

    return sorted(cluster_output, key=lambda x: (-x["page_count"], x["category"], x["topic"]))

# Backward-compatible entry point retained for older tests/callers only.
# The live sitemap job no longer uses this fixed BFSI mapping; it uses the
# domain-agnostic preliminary_topic() + build_dynamic_topic_taxonomy() pipeline.
_LEGACY_COMPAT_TOPIC_RULES = [
    ("Credit & Cards", "Credit Score", [r"credit score", r"cibil"]),
    ("Credit & Cards", "Credit Cards", [r"credit card"]),
    ("Loans", "Personal Loan", [r"personal loan"]),
    ("Loans", "Home Loan", [r"home loan", r"housing loan"]),
    ("Loans", "Gold Loan", [r"gold loan"]),
    ("Insurance", "Health Insurance", [r"health insurance"]),
    ("Insurance", "Term Insurance", [r"term insurance", r"term plan"]),
    ("Investments", "Mutual Funds", [r"mutual fund"]),
    ("Investments", "SIP", [r"systematic investment plan", r"\bsip\b"]),
    ("Investments", "Fixed Deposit", [r"fixed deposit", r"\bfd\b"]),
]

def classify_topic(evidence: dict) -> dict:
    pieces = [
        evidence.get("title") or "",
        " ".join(evidence.get("h1") or []),
        " ".join((evidence.get("h2") or [])[:12]),
        " ".join(evidence.get("breadcrumbs") or []),
        (evidence.get("text_excerpt") or "")[:2500],
    ]
    text = " ".join(pieces).casefold()
    for category, topic, patterns in _LEGACY_COMPAT_TOPIC_RULES:
        if any(re.search(pattern, text, flags=re.I) for pattern in patterns):
            return {
                "category": category,
                "topic": topic,
                "subtopic": "",
                "confidence": 0.8,
                "method": "legacy-compat",
            }
    return preliminary_topic(evidence)
