from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from statistics import median
from types import SimpleNamespace

from sqlalchemy import func, or_, tuple_

from app import models
from app.services.sitemap_duplicate_audit import (
    CANONICAL_COLLISION,
    REDIRECT_COLLISION,
    RESOLVED_IDENTITY_COLLISION,
    canonical_state,
)

CONTENT_TYPES = {
    "PRODUCT", "CONTENT", "BLOG", "CATEGORY", "TOOL_CALCULATOR", "FAQ_HELP", "LANDING_PAGE"
}
EXCLUDED_LABEL = "Other / Unclassified"


def _run_or_none(db, run_id: int):
    return db.get(models.SitemapRun, run_id)


def _sources(db, run):
    """Return the exact source metadata captured for this historical run.

    New runs persist an immutable `_input_snapshot.sources` list in config_json.
    That means renaming/reactivating a SitemapSource later cannot silently rewrite
    labels/roles in an older report.  Older runs fall back to their persisted
    SitemapUrl membership plus the current source rows.
    """
    run_source_ids = [
        row[0]
        for row in (
            db.query(models.SitemapUrl.source_id)
            .filter(models.SitemapUrl.sitemap_run_id == run.id)
            .distinct()
            .all()
        )
        if row[0] is not None
    ]

    snapshot = dict((run.config_json or {}).get("_input_snapshot") or {})
    source_rows = list(snapshot.get("sources") or [])

    if source_rows:
        rows = [
            SimpleNamespace(**item)
            for item in source_rows
            if item.get("id") is not None
            and (not run_source_ids or int(item["id"]) in run_source_ids)
        ]
    else:
        query = db.query(models.SitemapSource).filter(
            models.SitemapSource.project_id == run.project_id
        )
        if run_source_ids:
            query = query.filter(models.SitemapSource.id.in_(run_source_ids))
        rows = query.order_by(models.SitemapSource.id).all()

    by_id = {int(x.id): x for x in rows}
    client = next((x for x in rows if x.role == "CLIENT"), None)
    competitors = [x for x in rows if x.role == "COMPETITOR"]
    return by_id, client, competitors


def _audit_source_map(db, run) -> dict[int, object]:
    """Return every source captured for the run, including duplicate-only sources."""
    snapshot = dict((run.config_json or {}).get("_input_snapshot") or {})
    source_rows = list(snapshot.get("sources") or [])
    if source_rows:
        return {
            int(item["id"]): SimpleNamespace(**item)
            for item in source_rows
            if item.get("id") is not None
        }
    rows = (
        db.query(models.SitemapSource)
        .filter(models.SitemapSource.project_id == run.project_id)
        .order_by(models.SitemapSource.id)
        .all()
    )
    return {int(x.id): x for x in rows}


def _eligible_query(db, run_id: int):
    return db.query(models.SitemapUrl).filter(
        models.SitemapUrl.sitemap_run_id == run_id,
        models.SitemapUrl.error.is_(None),
        models.SitemapUrl.detailed_page_type.in_(CONTENT_TYPES),
        models.SitemapUrl.category.is_not(None),
        models.SitemapUrl.topic.is_not(None),
        models.SitemapUrl.category != EXCLUDED_LABEL,
        models.SitemapUrl.topic != EXCLUDED_LABEL,
    )


def _paged(items, total: int, page: int, page_size: int):
    return {
        "page": page,
        "page_size": page_size,
        "total": int(total),
        "items": items,
    }


def topic_taxonomy_page(db, run_id: int, page: int = 1, page_size: int = 100, q: str | None = None):
    run = _run_or_none(db, run_id)
    if not run:
        return _paged([], 0, 1, page_size)
    by_id, client, competitors = _sources(db, run)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    base = _eligible_query(db, run_id).with_entities(models.SitemapUrl.category, models.SitemapUrl.topic)
    if q:
        like = f"%{q.strip()}%"
        base = base.filter(or_(models.SitemapUrl.category.ilike(like), models.SitemapUrl.topic.ilike(like)))
    distinct = base.distinct()
    total = db.query(func.count()).select_from(distinct.subquery()).scalar() or 0
    pairs = distinct.order_by(models.SitemapUrl.category, models.SitemapUrl.topic).offset((page - 1) * page_size).limit(page_size).all()
    pair_values = [(x[0], x[1]) for x in pairs]
    if not pair_values:
        return _paged([], total, page, page_size)

    rows = (
        _eligible_query(db, run_id)
        .with_entities(
            models.SitemapUrl.category,
            models.SitemapUrl.topic,
            models.SitemapUrl.source_id,
            models.SitemapUrl.subtopic,
        )
        .filter(tuple_(models.SitemapUrl.category, models.SitemapUrl.topic).in_(pair_values))
        .all()
    )
    counts = defaultdict(Counter)
    subtopics = defaultdict(set)
    for category, topic, source_id, subtopic in rows:
        counts[(category, topic)][source_id] += 1
        if subtopic and len(subtopics[(category, topic)]) < 25:
            subtopics[(category, topic)].add(str(subtopic).strip())

    items = []
    competitor_ids = [x.id for x in competitors]
    for category, topic in pair_values:
        c = counts[(category, topic)]
        items.append({
            "Category": category,
            "Topic": topic,
            "Subtopics": " | ".join(sorted(x for x in subtopics[(category, topic)] if x)),
            "Client URLs": c.get(client.id, 0) if client else 0,
            "Competitor URLs": sum(c.get(cid, 0) for cid in competitor_ids),
        })
    return _paged(items, total, page, page_size)



def cluster_audit_page(db, run_id: int, page: int = 1, page_size: int = 100, q: str | None = None):
    """Rebuild the canonical-cluster audit view lazily from persisted URLs.

    This replaces the old 5,000-row JSON snapshot, so large runs stay fully
    navigable without bloating SitemapRun.analysis_json.
    """
    run = _run_or_none(db, run_id)
    if not run:
        return _paged([], 0, 1, page_size)

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    base = _eligible_query(db, run_id).with_entities(
        models.SitemapUrl.category,
        models.SitemapUrl.topic,
    )
    if q:
        like = f"%{q.strip()}%"
        base = base.filter(
            or_(
                models.SitemapUrl.category.ilike(like),
                models.SitemapUrl.topic.ilike(like),
                models.SitemapUrl.subtopic.ilike(like),
            )
        )

    distinct = base.distinct()
    total = db.query(func.count()).select_from(distinct.subquery()).scalar() or 0
    pairs = (
        distinct
        .order_by(models.SitemapUrl.category, models.SitemapUrl.topic)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    pair_values = [(x[0], x[1]) for x in pairs]
    if not pair_values:
        return _paged([], total, page, page_size)

    rows = (
        _eligible_query(db, run_id)
        .with_entities(
            models.SitemapUrl.category,
            models.SitemapUrl.topic,
            models.SitemapUrl.subtopic,
            models.SitemapUrl.topic_confidence,
            models.SitemapUrl.topic_method,
        )
        .filter(tuple_(models.SitemapUrl.category, models.SitemapUrl.topic).in_(pair_values))
        .all()
    )

    counts = Counter()
    confidence_sum = defaultdict(float)
    methods = defaultdict(Counter)
    variants = defaultdict(set)

    for category, topic, subtopic, confidence, method in rows:
        key = (category, topic)
        counts[key] += 1
        confidence_sum[key] += float(confidence or 0)
        methods[key][str(method or "unknown")] += 1
        if subtopic and len(variants[key]) < 25:
            variants[key].add(str(subtopic).strip())

    items = []
    for category, topic in pair_values:
        key = (category, topic)
        page_count = int(counts[key])
        raw = f"{category}\x1f{topic}".encode("utf-8", "ignore")
        items.append({
            "cluster_id": "REC-" + hashlib.sha1(raw).hexdigest()[:12],
            "category": category,
            "topic": topic,
            "confidence": round(confidence_sum[key] / max(page_count, 1), 3),
            "method": methods[key].most_common(1)[0][0] if methods[key] else "recovered",
            "page_count": page_count,
            "variant_count": len(variants[key]),
            "variants": " | ".join(sorted(x for x in variants[key] if x)[:10]),
        })

    return _paged(items, total, page, page_size)


def topic_coverage_page(db, run_id: int, page: int = 1, page_size: int = 100, q: str | None = None):
    run = _run_or_none(db, run_id)
    if not run:
        return _paged([], 0, 1, page_size)
    _, client, competitors = _sources(db, run)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    base = _eligible_query(db, run_id).with_entities(models.SitemapUrl.topic)
    if q:
        base = base.filter(models.SitemapUrl.topic.ilike(f"%{q.strip()}%"))
    distinct = base.distinct()
    total = db.query(func.count()).select_from(distinct.subquery()).scalar() or 0
    topics = [x[0] for x in distinct.order_by(models.SitemapUrl.topic).offset((page - 1) * page_size).limit(page_size).all()]
    if not topics:
        return _paged([], total, page, page_size)

    grouped = (
        _eligible_query(db, run_id)
        .with_entities(models.SitemapUrl.topic, models.SitemapUrl.source_id, func.count(models.SitemapUrl.id))
        .filter(models.SitemapUrl.topic.in_(topics))
        .group_by(models.SitemapUrl.topic, models.SitemapUrl.source_id)
        .all()
    )
    counts = defaultdict(Counter)
    for topic, source_id, count in grouped:
        counts[topic][source_id] = int(count)

    items = []
    for topic in topics:
        c = counts[topic]
        comp_values = [c.get(x.id, 0) for x in competitors]
        row = {"Topic": topic, "Client Count": c.get(client.id, 0) if client else 0}
        for source in competitors:
            row[source.label] = c.get(source.id, 0)
        comp_median = float(median(comp_values)) if comp_values else 0.0
        client_count = int(row["Client Count"])
        row.update({
            "Competitor Total": sum(comp_values),
            "Competitor Average": round(sum(comp_values) / max(len(comp_values), 1), 2),
            "Competitor Median": comp_median,
            "Competitor Max": max(comp_values) if comp_values else 0,
            "Competitor Sites Covering": sum(1 for x in comp_values if x > 0),
            "Competitor Support %": round(sum(1 for x in comp_values if x > 0) / max(len(comp_values), 1) * 100, 1) if comp_values else 0,
            "Client Gap vs Competitor Median": round(client_count - comp_median, 2),
        })
        items.append(row)
    return _paged(items, total, page, page_size)


def head_to_head_page(db, run_id: int, page: int = 1, page_size: int = 100, q: str | None = None):
    run = _run_or_none(db, run_id)
    if not run:
        return _paged([], 0, 1, page_size)
    _, client, competitors = _sources(db, run)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    base = _eligible_query(db, run_id).with_entities(models.SitemapUrl.topic, models.SitemapUrl.detailed_page_type)
    if q:
        like = f"%{q.strip()}%"
        base = base.filter(or_(models.SitemapUrl.topic.ilike(like), models.SitemapUrl.detailed_page_type.ilike(like)))
    distinct = base.distinct()
    total = db.query(func.count()).select_from(distinct.subquery()).scalar() or 0
    pairs = distinct.order_by(models.SitemapUrl.topic, models.SitemapUrl.detailed_page_type).offset((page - 1) * page_size).limit(page_size).all()
    pair_values = [(x[0], x[1]) for x in pairs]
    if not pair_values:
        return _paged([], total, page, page_size)

    grouped = (
        _eligible_query(db, run_id)
        .with_entities(
            models.SitemapUrl.topic,
            models.SitemapUrl.detailed_page_type,
            models.SitemapUrl.source_id,
            func.count(models.SitemapUrl.id),
        )
        .filter(tuple_(models.SitemapUrl.topic, models.SitemapUrl.detailed_page_type).in_(pair_values))
        .group_by(models.SitemapUrl.topic, models.SitemapUrl.detailed_page_type, models.SitemapUrl.source_id)
        .all()
    )
    counts = defaultdict(Counter)
    for topic, page_type, source_id, count in grouped:
        counts[(topic, page_type)][source_id] = int(count)

    items = []
    for topic, page_type in pair_values:
        c = counts[(topic, page_type)]
        comp_values = [c.get(x.id, 0) for x in competitors]
        row = {
            "Topic": topic,
            "Page Type": "CONTENT" if page_type == "BLOG" else page_type,
            "Client Count": c.get(client.id, 0) if client else 0,
        }
        for source in competitors:
            row[source.label] = c.get(source.id, 0)
        row.update({
            "Competitor Total": sum(comp_values),
            "Competitor Median": float(median(comp_values)) if comp_values else 0.0,
            "Competitor Sites Covering": sum(1 for x in comp_values if x > 0),
            "Competitor Support %": round(sum(1 for x in comp_values if x > 0) / max(len(comp_values), 1) * 100, 1) if comp_values else 0,
        })
        items.append(row)
    return _paged(items, total, page, page_size)


def missing_categories_all(db, run_id: int, q: str | None = None) -> list[dict]:
    run = _run_or_none(db, run_id)
    if not run:
        return []
    _, client, competitors = _sources(db, run)
    grouped = (
        _eligible_query(db, run_id)
        .with_entities(
            models.SitemapUrl.category,
            models.SitemapUrl.source_id,
            models.SitemapUrl.detailed_page_type,
            func.count(models.SitemapUrl.id),
        )
        .group_by(models.SitemapUrl.category, models.SitemapUrl.source_id, models.SitemapUrl.detailed_page_type)
        .all()
    )
    by_category = defaultdict(lambda: defaultdict(Counter))
    for category, source_id, page_type, count in grouped:
        by_category[category][source_id][page_type] = int(count)

    items = []
    for category in sorted(by_category):
        if q and q.strip().casefold() not in category.casefold():
            continue
        source_counts = by_category[category]
        client_total = sum(source_counts.get(client.id, {}).values()) if client else 0
        client_hubs = source_counts.get(client.id, {}).get("CATEGORY", 0) if client else 0
        comp_sites = sum(1 for x in competitors if sum(source_counts.get(x.id, {}).values()) > 0)
        comp_hub_sites = sum(1 for x in competitors if source_counts.get(x.id, {}).get("CATEGORY", 0) > 0)
        comp_hub_urls = sum(source_counts.get(x.id, {}).get("CATEGORY", 0) for x in competitors)
        if client_hubs == 0 and comp_hub_sites > 0:
            items.append({
                "Category": category,
                "Client Category URLs": client_total,
                "Client Category/Hub Pages": client_hubs,
                "Competitor Sites Covering Category": comp_sites,
                "Competitor Sites With Category/Hub": comp_hub_sites,
                "Competitor Category/Hub URLs": comp_hub_urls,
                "Competitor Support %": round(comp_hub_sites / max(len(competitors), 1) * 100, 1),
                "Business Relevance": "high" if client_total > 0 else "uncertain",
                "Suggested Page Type": "CATEGORY",
                "Status": "Missing category/hub structure",
            })
    return items


def missing_categories_page(db, run_id: int, page: int = 1, page_size: int = 100, q: str | None = None):
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))
    items = missing_categories_all(db, run_id, q)
    total = len(items)
    start = (page - 1) * page_size
    return _paged(items[start:start + page_size], total, page, page_size)


def opportunities_page(
    db,
    run_id: int,
    page: int = 1,
    page_size: int = 50,
    priority: str | None = None,
    gap_type: str | None = None,
    q: str | None = None,
):
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    query = db.query(models.SitemapOpportunity).filter(models.SitemapOpportunity.sitemap_run_id == run_id)
    if priority:
        query = query.filter(models.SitemapOpportunity.priority == priority.upper())
    if gap_type:
        query = query.filter(models.SitemapOpportunity.gap_type == gap_type.upper())
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            models.SitemapOpportunity.title.ilike(like),
            models.SitemapOpportunity.category.ilike(like),
            models.SitemapOpportunity.topic.ilike(like),
        ))
    total = query.count()
    rows = (
        query.order_by(
            models.SitemapOpportunity.priority,
            models.SitemapOpportunity.confidence.desc(),
            models.SitemapOpportunity.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [{
        "id": x.id,
        "opportunity_key": x.opportunity_key,
        "gap_type": x.gap_type,
        "priority": x.priority,
        "confidence": x.confidence,
        "category": x.category,
        "topic": x.topic,
        "recommended_page_type": x.recommended_page_type,
        "client_count": x.client_count,
        "competitor_count": x.competitor_count,
        "competitor_sites": x.competitor_sites,
        "business_relevance": x.business_relevance,
        "title": x.title,
        "recommendation": x.recommendation,
        "evidence_ids": x.evidence_json,
        "source": "ai-reviewed" if "ai" in str(x.source or "").lower() else x.source,
    } for x in rows]
    return _paged(items, total, page, page_size)



def _historical_duplicate_count(run) -> int:
    cfg = dict(run.config_json or {})
    source_rows = list(
        cfg.get("_sitemap_discovery")
        or (run.analysis_json or {}).get("source_discovery")
        or []
    )
    return sum(int(x.get("duplicate_url_records") or 0) for x in source_rows)


def duplicate_audit_page(
    db,
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    duplicate_kind: str | None = None,
    source_id: int | None = None,
    q: str | None = None,
):
    run = _run_or_none(db, run_id)
    if not run:
        return {
            "page": 1,
            "page_size": page_size,
            "total": 0,
            "items": [],
            "summary": {},
        }

    by_id = _audit_source_map(db, run)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    all_rows = db.query(models.SitemapDuplicate).filter(
        models.SitemapDuplicate.sitemap_run_id == run_id
    )

    kind_counts = {
        str(kind): int(count)
        for kind, count in (
            all_rows.with_entities(
                models.SitemapDuplicate.duplicate_kind,
                func.count(models.SitemapDuplicate.id),
            )
            .group_by(models.SitemapDuplicate.duplicate_kind)
            .all()
        )
    }
    total_records = int(sum(kind_counts.values()))
    duplicate_groups = int(
        all_rows.with_entities(
            func.count(func.distinct(models.SitemapDuplicate.group_key))
        ).scalar()
        or 0
    )

    query = all_rows
    if duplicate_kind and duplicate_kind.strip():
        query = query.filter(
            models.SitemapDuplicate.duplicate_kind == duplicate_kind.strip().upper()
        )
    if source_id is not None:
        query = query.filter(
            or_(
                models.SitemapDuplicate.primary_source_id == int(source_id),
                models.SitemapDuplicate.duplicate_source_id == int(source_id),
            )
        )
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                models.SitemapDuplicate.match_url.ilike(like),
                models.SitemapDuplicate.primary_url.ilike(like),
                models.SitemapDuplicate.duplicate_url.ilike(like),
                models.SitemapDuplicate.primary_child_sitemap.ilike(like),
                models.SitemapDuplicate.duplicate_child_sitemap.ilike(like),
                models.SitemapDuplicate.primary_canonical_url.ilike(like),
                models.SitemapDuplicate.duplicate_canonical_url.ilike(like),
            )
        )

    filtered_total = int(query.count())
    rows = (
        query.order_by(models.SitemapDuplicate.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = []
    for row in rows:
        primary_source = by_id.get(int(row.primary_source_id))
        duplicate_source = by_id.get(int(row.duplicate_source_id))
        items.append({
            "id": row.id,
            "duplicate_kind": row.duplicate_kind,
            "audit_stage": row.audit_stage,
            "group_key": row.group_key,
            "match_url": row.match_url,
            "primary_site": getattr(primary_source, "label", None) or row.primary_source_id,
            "primary_role": getattr(primary_source, "role", "") or "",
            "duplicate_site": getattr(duplicate_source, "label", None) or row.duplicate_source_id,
            "duplicate_role": getattr(duplicate_source, "role", "") or "",
            "primary_url": row.primary_url,
            "duplicate_url": row.duplicate_url,
            "primary_child_sitemap": row.primary_child_sitemap,
            "duplicate_child_sitemap": row.duplicate_child_sitemap,
            "primary_final_url": row.primary_final_url,
            "duplicate_final_url": row.duplicate_final_url,
            "primary_canonical_url": row.primary_canonical_url,
            "duplicate_canonical_url": row.duplicate_canonical_url,
            "primary_http_status": row.primary_http_status,
            "duplicate_http_status": row.duplicate_http_status,
            "primary_lastmod": row.primary_lastmod,
            "duplicate_lastmod": row.duplicate_lastmod,
        })

    audit_meta = dict((run.config_json or {}).get("_duplicate_audit") or {})
    historical_count = _historical_duplicate_count(run)
    if total_records > 0:
        evidence_status = "complete"
    elif audit_meta.get("version"):
        evidence_status = "complete_no_duplicates"
    elif historical_count > 0:
        evidence_status = "historical_count_only"
    else:
        evidence_status = "historical_or_no_duplicates"

    return {
        "page": page,
        "page_size": page_size,
        "total": filtered_total,
        "items": items,
        "summary": {
            "duplicate_records": total_records,
            "duplicate_groups": duplicate_groups,
            "sitemap_exact_duplicates": int(kind_counts.get("SITEMAP_EXACT_DUPLICATE", 0)),
            "sitemap_normalized_duplicates": int(kind_counts.get("SITEMAP_NORMALIZED_DUPLICATE", 0)),
            "cross_source_duplicates": int(kind_counts.get("CROSS_SOURCE_DUPLICATE", 0)),
            "canonical_collisions": int(kind_counts.get(CANONICAL_COLLISION, 0)),
            "redirect_collisions": int(kind_counts.get(REDIRECT_COLLISION, 0)),
            "resolved_identity_collisions": int(kind_counts.get(RESOLVED_IDENTITY_COLLISION, 0)),
            "historical_duplicate_count": int(historical_count),
            "evidence_status": evidence_status,
            "audit_version": audit_meta.get("version"),
            "audit_mode": audit_meta.get("mode"),
        },
    }


def canonical_audit_page(
    db,
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    source_id: int | None = None,
    q: str | None = None,
):
    run = _run_or_none(db, run_id)
    if not run:
        return {
            "page": 1,
            "page_size": page_size,
            "total": 0,
            "items": [],
            "summary": {},
        }

    by_id, _, _ = _sources(db, run)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    all_rows = db.query(models.SitemapUrl).filter(
        models.SitemapUrl.sitemap_run_id == run_id
    )
    total_urls = int(all_rows.count())
    canonical_present = int(
        all_rows.filter(
            models.SitemapUrl.canonical_url.is_not(None),
            models.SitemapUrl.canonical_url != "",
        ).count()
    )
    canonical_missing = max(0, total_urls - canonical_present)

    query = all_rows
    if source_id is not None:
        query = query.filter(models.SitemapUrl.source_id == int(source_id))
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                models.SitemapUrl.original_url.ilike(like),
                models.SitemapUrl.normalized_url.ilike(like),
                models.SitemapUrl.final_url.ilike(like),
                models.SitemapUrl.canonical_url.ilike(like),
                models.SitemapUrl.title.ilike(like),
            )
        )

    filtered_total = int(query.count())
    rows = (
        query.order_by(models.SitemapUrl.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    collision_counts = {
        str(kind): int(count)
        for kind, count in (
            db.query(
                models.SitemapDuplicate.duplicate_kind,
                func.count(models.SitemapDuplicate.id),
            )
            .filter(
                models.SitemapDuplicate.sitemap_run_id == run_id,
                models.SitemapDuplicate.duplicate_kind.in_(
                    [CANONICAL_COLLISION, REDIRECT_COLLISION, RESOLVED_IDENTITY_COLLISION]
                ),
            )
            .group_by(models.SitemapDuplicate.duplicate_kind)
            .all()
        )
    }

    items = []
    for row in rows:
        source = by_id.get(int(row.source_id))
        items.append({
            "id": row.id,
            "site": getattr(source, "label", None) or row.source_id,
            "role": getattr(source, "role", "") or "",
            "root_sitemap": row.root_sitemap,
            "child_sitemap": row.child_sitemap,
            "original_url": row.original_url,
            "normalized_url": row.normalized_url,
            "final_url": row.final_url,
            "canonical_url": row.canonical_url,
            "canonical_state": canonical_state(row),
            "http_status": row.http_status,
            "lastmod": row.lastmod,
            "fetch_method": row.fetch_method,
            "error": row.error,
        })

    audit_meta = dict((run.config_json or {}).get("_duplicate_audit") or {})
    return {
        "page": page,
        "page_size": page_size,
        "total": filtered_total,
        "items": items,
        "summary": {
            "inventory_urls": total_urls,
            "canonical_present": canonical_present,
            "canonical_missing": canonical_missing,
            "canonical_collisions": int(collision_counts.get(CANONICAL_COLLISION, 0)),
            "redirect_collisions": int(collision_counts.get(REDIRECT_COLLISION, 0)),
            "resolved_identity_collisions": int(collision_counts.get(RESOLVED_IDENTITY_COLLISION, 0)),
            "audit_version": audit_meta.get("version"),
        },
    }


def failures_page(db, run_id: int, page: int = 1, page_size: int = 100, severity: str | None = None, q: str | None = None):
    run = _run_or_none(db, run_id)
    if not run:
        return _paged([], 0, 1, page_size)
    sources, _, _ = _sources(db, run)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))
    query = db.query(models.SitemapUrl).filter(
        models.SitemapUrl.sitemap_run_id == run_id,
        models.SitemapUrl.error.is_not(None),
    )
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(models.SitemapUrl.original_url.ilike(like), models.SitemapUrl.error.ilike(like)))
    total = query.count()
    rows = query.order_by(models.SitemapUrl.id).offset((page - 1) * page_size).limit(page_size).all()
    items = []
    for x in rows:
        source = sources.get(x.source_id)
        evidence = x.classification_evidence_json or {}
        terminal = str(evidence.get("terminal_state") or "") if isinstance(evidence, dict) else ""
        stale = x.http_status in {404, 410}
        pending = terminal == "pending_resolution"
        challenge = terminal == "security_challenge" or "challenge" in str(x.error or "").lower()
        if pending:
            code, sev = "PENDING_RESOLUTION", "warning"
        elif stale:
            code, sev = "STALE_SITEMAP_URL", "warning"
        elif challenge:
            code, sev = "SECURITY_CHALLENGE", "error"
        else:
            code, sev = "PAGE_RESOLUTION_FAILED", "error"
        if severity and sev != severity.lower():
            continue
        items.append({
            "site": source.label if source else x.source_id,
            "role": source.role if source else "",
            "stage": "page_resolution",
            "url": x.original_url,
            "http_status": x.http_status,
            "retry_count": x.retry_count,
            "terminal_state": terminal or None,
            "severity": sev,
            "code": code,
            "error": f"Sitemap URL returned HTTP {x.http_status} after automatic verification" if stale else x.error,
        })
    return _paged(items, total, page, page_size)


def result_quality(db, run_id: int, opportunity_count: int | None = None) -> dict:
    eligible = _eligible_query(db, run_id)
    eligible_count = eligible.count()
    distinct_topics = db.query(func.count()).select_from(
        eligible.with_entities(models.SitemapUrl.topic).distinct().subquery()
    ).scalar() or 0
    if opportunity_count is None:
        opportunity_count = db.query(models.SitemapOpportunity).filter_by(sitemap_run_id=run_id).count()
    topic_uniqueness_ratio = round(distinct_topics / max(eligible_count, 1), 4)
    opportunity_ratio = round(int(opportunity_count) / max(distinct_topics, 1), 4)
    warnings = []
    if eligible_count >= 100 and topic_uniqueness_ratio > 0.45:
        warnings.append(
            f"Topic taxonomy is highly fragmented: {distinct_topics:,} distinct topics across {eligible_count:,} eligible content URLs ({topic_uniqueness_ratio:.1%})."
        )
    if int(opportunity_count) >= 1000 and opportunity_ratio > 0.75:
        warnings.append(
            f"Opportunity volume is unusually close to topic volume: {int(opportunity_count):,} opportunities for {distinct_topics:,} topics ({opportunity_ratio:.1%})."
        )
    return {
        "eligible_content_urls": int(eligible_count),
        "distinct_topics": int(distinct_topics),
        "opportunities": int(opportunity_count),
        "topic_uniqueness_ratio": topic_uniqueness_ratio,
        "opportunity_to_topic_ratio": opportunity_ratio,
        "status": "warning" if warnings else "ok",
        "warnings": warnings,
    }


def compact_analysis_payload(analysis: dict, *, quality: dict | None = None, opportunity_count: int | None = None) -> dict:
    """Keep only small run metadata in SitemapRun.analysis_json.

    Large matrices are served lazily from SitemapUrl/SitemapOpportunity tables.
    This prevents every run poll from deserializing hundreds of MB of JSON.
    """
    counts = dict(analysis.get("result_counts") or {})
    counts.update({
        "topic_taxonomy": len(analysis.get("topic_taxonomy") or []),
        "topic_clusters": int(analysis.get("dynamic_topic_clusters_total") or len(analysis.get("dynamic_topic_clusters") or [])),
        "topic_coverage": len(analysis.get("topic_coverage_matrix") or []),
        "head_to_head": len(analysis.get("head_to_head") or []),
        "missing_categories": len(analysis.get("missing_categories") or []),
        "opportunities": int(opportunity_count if opportunity_count is not None else len(analysis.get("content_opportunities") or [])),
    })
    # Deliberately keep only small metadata here. Large analytical views are
    # reconstructed from SitemapUrl / SitemapOpportunity / SitemapDuplicate via
    # the paginated result-view functions in this module.
    compact = {
        "overview": analysis.get("overview") or [],
        "site_profiles": analysis.get("site_profiles") or [],
        "url_type_summary": analysis.get("url_type_summary") or [],
        "comparison_status": analysis.get("comparison_status") or {},
        "source_discovery": analysis.get("source_discovery") or [],
        "discovery_scope": analysis.get("discovery_scope") or analysis.get("source_discovery") or [],
        "dynamic_topic_clusters_total": counts["topic_clusters"],
        "result_counts": counts,
        "quality": quality or analysis.get("quality") or {},
        "coverage_recovery": analysis.get("coverage_recovery") or {},
        # Discovery issues are small and contain exact sitemap URLs that do not
        # belong in SitemapUrl because they are sitemap-document audit evidence.
        "sitemap_failures": analysis.get("sitemap_failures") or [],
        "recovery_summary": analysis.get("recovery_summary") or {},
        "resolution_summary": analysis.get("resolution_summary") or {},
    }
    return compact

################################################################################
# FRONTEND
################################################################################


def failure_page(
    db,
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    http_status: int | None = None,
    host: str | None = None,
    q: str | None = None,
):
    """Paginate large page-resolution failure sets without returning giant JSON."""
    run = _run_or_none(db, run_id)
    if not run:
        return {
            "page": 1,
            "page_size": page_size,
            "total": 0,
            "items": [],
            "supplemental": [],
            "summary": {},
        }

    by_id, _, _ = _sources(db, run)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    all_failures = db.query(models.SitemapUrl).filter(
        models.SitemapUrl.sitemap_run_id == run_id,
        models.SitemapUrl.error.is_not(None),
    )

    grouped = (
        all_failures.with_entities(
            models.SitemapUrl.http_status,
            func.count(models.SitemapUrl.id),
        )
        .group_by(models.SitemapUrl.http_status)
        .all()
    )
    status_counts = {
        "transport/no-status" if status is None else str(int(status)): int(count)
        for status, count in grouped
    }
    db_failure_total = sum(status_counts.values())
    stale_total = sum(status_counts.get(str(x), 0) for x in (404, 410))
    host_access_deferred_total = all_failures.filter(
        models.SitemapUrl.error.ilike("Deferred after host access circuit opened%")
    ).count()
    recoverable_statuses = {401, 403, 406, 408, 425, 429, 500, 502, 503, 504}
    recoverable_total = (
        all_failures.filter(
            models.SitemapUrl.error != "Pending page resolution",
            or_(
                models.SitemapUrl.http_status.in_(recoverable_statuses),
                models.SitemapUrl.http_status.is_(None),
                models.SitemapUrl.error.ilike("%challenge%"),
                models.SitemapUrl.error.ilike("%security%"),
            )
        ).count()
    )

    query = all_failures
    if http_status is not None:
        query = query.filter(models.SitemapUrl.http_status == int(http_status))
    if host and host.strip():
        host_like = f"%{host.strip()}%"
        query = query.filter(
            or_(
                models.SitemapUrl.original_url.ilike(host_like),
                models.SitemapUrl.final_url.ilike(host_like),
            )
        )
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                models.SitemapUrl.original_url.ilike(like),
                models.SitemapUrl.final_url.ilike(like),
                models.SitemapUrl.error.ilike(like),
                models.SitemapUrl.title.ilike(like),
            )
        )

    total = query.count()
    rows = (
        query.order_by(models.SitemapUrl.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    from urllib.parse import urlparse

    items = []
    for row in rows:
        source = by_id.get(int(row.source_id))
        evidence = row.classification_evidence_json or {}
        terminal = str(evidence.get("terminal_state") or "")
        stale = row.http_status in {404, 410}
        pending = terminal == "pending_resolution"
        challenge = terminal == "security_challenge" or "challenge" in str(row.error or "").lower()
        if pending:
            code, severity = "PENDING_RESOLUTION", "warning"
        elif stale:
            code, severity = "STALE_SITEMAP_URL", "warning"
        elif terminal == "host_access_deferred":
            code, severity = "HOST_ACCESS_DEFERRED", "warning"
        elif challenge:
            code, severity = "SECURITY_CHALLENGE", "error"
        elif row.http_status == 429:
            code, severity = "RATE_LIMITED", "error"
        else:
            code, severity = "PAGE_RESOLUTION_FAILED", "error"
        original = row.original_url or ""
        items.append({
            "site": getattr(source, "label", None) or row.source_id,
            "role": getattr(source, "role", "") or "",
            "stage": "page_resolution",
            "root_sitemap": row.root_sitemap,
            "parent_sitemap": row.child_sitemap,
            "child_sitemap": row.child_sitemap,
            "host": (urlparse(original).hostname or "").lower(),
            "url": original,
            "http_status": row.http_status,
            "retry_count": row.retry_count,
            "terminal_state": terminal or None,
            "severity": severity,
            "code": code,
            "fetch_method": row.fetch_method,
            "error": row.error,
        })

    supplemental = []
    analysis = run.analysis_json or {}
    discovery_issues = (
        analysis.get("sitemap_failures")
        or (run.config_json or {}).get("_sitemap_discovery_issues")
        or []
    )
    for issue in discovery_issues:
        source = by_id.get(int(issue.get("source_id") or 0))
        supplemental.append({
            "site": issue.get("site") or getattr(source, "label", None) or "—",
            "role": issue.get("role") or getattr(source, "role", "") or "",
            "stage": issue.get("stage") or "sitemap_discovery",
            "root_sitemap": issue.get("root_sitemap"),
            "parent_sitemap": issue.get("parent_sitemap"),
            "child_sitemap": issue.get("url") or issue.get("sitemap"),
            "host": "",
            "url": issue.get("url") or issue.get("sitemap") or issue.get("root_sitemap") or "—",
            "http_status": issue.get("http_status"),
            "retry_count": issue.get("retry_count", 0),
            "verification_attempts": issue.get("verification_attempts", 0),
            "empty_kind": issue.get("empty_kind"),
            "terminal_state": None,
            "severity": issue.get("severity") or "error",
            "code": issue.get("code") or "SITEMAP_DISCOVERY_ISSUE",
            "fetch_method": None,
            "error": issue.get("error") or "Unknown sitemap issue",
        })

    for issue in analysis.get("failure_summary", []):
        if issue.get("Stage") != "page_browser_enrichment":
            continue
        supplemental.append({
            "site": issue.get("Site") or "—",
            "role": "",
            "stage": issue.get("Stage"),
            "host": "",
            "url": issue.get("URL") or "—",
            "http_status": issue.get("HTTP Status"),
            "retry_count": 0,
            "terminal_state": None,
            "severity": issue.get("Severity") or "warning",
            "code": issue.get("Code") or "BROWSER_ENRICHMENT_WARNING",
            "fetch_method": None,
            "error": issue.get("Error") or "Browser enrichment warning",
        })

    return {
        "page": page,
        "page_size": page_size,
        "total": int(total),
        "items": items,
        "supplemental": supplemental,
        "summary": {
            "page_resolution_failures": int(db_failure_total),
            "supplemental_issues": len(supplemental),
            "total_failures_and_warnings": int(db_failure_total + len(supplemental)),
            "recoverable": int(recoverable_total),
            "host_access_deferred": int(host_access_deferred_total),
            "stale_404_410": int(stale_total),
            "rate_limited_429": int(status_counts.get("429", 0)),
            "status_counts": status_counts,
        },
    }


