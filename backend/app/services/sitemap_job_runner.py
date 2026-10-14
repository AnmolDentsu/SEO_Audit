from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

from sqlalchemy import delete

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.sitemap_ai import classify_ambiguous_pages_ai, review_sitemap_opportunities_ai
from app.services.sitemap_analysis import build_sitemap_analysis
from app.services.sitemap_classifier import broad_page_type, classify_page, needs_ai_review, needs_browser_fallback
from app.services.sitemap_fetcher import discover_sitemap_inventory, normalize_url, select_resolution_entries
from app.services.sitemap_duplicate_audit import (
    add_cross_source_duplicate,
    add_discovery_duplicates,
    add_resolved_collision,
)
from app.services.sitemap_fallback import (
    select_browser_fallback_candidates,
    should_impersonation_fallback,
)
from app.services.sitemap_page_resolver import (
    render_pages_browser,
    resolve_pages_http,
    resolve_pages_impersonated,
)
from app.services.sitemap_topics import build_dynamic_topic_taxonomy, preliminary_topic
from app.services.sitemap_result_views import compact_analysis_payload, result_quality

settings = get_settings()
logger = logging.getLogger(__name__)
_PROGRESS_SCALE = 1000


class SitemapRunCancellationRequested(Exception):
    pass


def _external_cancel_requested(run_id: int) -> bool:
    control_db = SessionLocal()
    try:
        row = control_db.get(models.SitemapRun, run_id)
        if not row:
            return True
        if str(row.status or "").strip().casefold() in {"cancelling", "cancelled"}:
            return True
        control = dict((row.config_json or {}).get("_control") or {})
        return bool(control.get("cancel_requested"))
    finally:
        control_db.close()


def _now():
    return datetime.now(timezone.utc)


def _progress(db, run, phase: str, start: float, end: float, fraction: float, message: str, current=None, total=None):
    """Persist weighted overall progress plus truthful current-stage progress/ETA."""
    if _external_cancel_requested(run.id):
        raise SitemapRunCancellationRequested(
            f"Sitemap run {run.id} cancellation requested"
        )

    now = _now()
    fraction = max(0.0, min(1.0, float(fraction)))
    overall_percent = start + (end - start) * fraction
    cfg = dict(run.config_json or {})
    old = dict(cfg.get("_progress") or {})
    overall_percent = max(float(old.get("percent") or 0), overall_percent)

    started = run.started_at
    if started and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0

    if old.get("stage_key") != phase:
        stage_started = now
    else:
        raw_stage_started = old.get("stage_started_at")
        try:
            stage_started = datetime.fromisoformat(raw_stage_started) if raw_stage_started else now
            if stage_started.tzinfo is None:
                stage_started = stage_started.replace(tzinfo=timezone.utc)
        except Exception:
            stage_started = now
    stage_elapsed = max(0.0, (now - stage_started).total_seconds())
    stage_percent = round(fraction * 100, 2)
    stage_rate = None
    stage_eta = None
    if current is not None and total is not None and int(current) > 0 and stage_elapsed > 0:
        stage_rate = float(current) / stage_elapsed
        if stage_rate > 0 and int(total) >= int(current):
            stage_eta = int((int(total) - int(current)) / stage_rate)

    cfg["_progress"] = {
        "percent": round(overall_percent, 1),
        "overall_percent": round(overall_percent, 1),
        "stage_percent": stage_percent,
        "stage_key": phase,
        "stage_label": phase.replace("_", " ").title(),
        "message": message,
        "stage_current": current,
        "stage_total": total,
        "elapsed_seconds": int(elapsed),
        "stage_elapsed_seconds": int(stage_elapsed),
        "stage_rate_per_second": round(stage_rate, 4) if stage_rate else None,
        "stage_eta_seconds": stage_eta,
        # Backward-compatible field used by the existing frontend.
        "eta_seconds": stage_eta,
        "stage_started_at": stage_started.isoformat(),
        "heartbeat_at": now.isoformat(),
    }
    run.config_json = cfg
    run.phase = phase
    run.progress_current = int(round(overall_percent / 100 * _PROGRESS_SCALE))
    run.progress_total = _PROGRESS_SCALE
    db.commit()


def _discovery_status(entries, errors: list[dict], warnings: list[dict], stats: dict) -> str:
    """Map discovery evidence to a source status without penalising empty children."""
    if entries and errors:
        return "partial"
    if entries and warnings:
        return "warning"
    if entries:
        return "success"
    if errors:
        return "failed"
    if int(stats.get("empty_child_sitemaps") or 0) > 0 and bool(stats.get("root_document_parsed")):
        return "warning"
    return "failed"


def _background_recovery_metadata(run, recoverable_count: int) -> dict:
    """Build durable automatic-recovery metadata without changing run status."""
    count = max(0, int(recoverable_count))
    if count <= 0:
        return {
            "version": 1,
            "automatic": True,
            "state": "completed",
            "completed_at": _now().isoformat(),
            "recoverable_rows": 0,
            "slices_completed": 0,
            "base_status": run.status,
            "base_phase": run.phase,
        }
    due = _now() + timedelta(
        seconds=max(0.0, float(settings.sitemap_background_recovery_initial_delay_seconds))
    )
    return {
        "version": 1,
        "automatic": True,
        "state": "queued",
        "queued_at": _now().isoformat(),
        "next_attempt_at": due.isoformat(),
        "base_status": run.status,
        "base_phase": run.phase,
        "recoverable_rows": count,
        "slices_completed": 0,
    }


def _store_live_discovery(db, run, rows: list[dict]):
    cfg = dict(run.config_json or {})
    cfg["_sitemap_discovery"] = rows
    cfg["_sitemap_discovery_totals"] = {
        "raw_url_records": sum(int(x.get("raw_url_records") or 0) for x in rows),
        "unique_sitemap_urls": sum(int(x.get("unique_sitemap_urls") or 0) for x in rows),
        "duplicate_url_records": sum(int(x.get("duplicate_url_records") or 0) for x in rows),
        "in_scope_urls": sum(int(x.get("in_scope_urls") or 0) for x in rows),
        "excluded_by_scope_urls": sum(int(x.get("excluded_by_scope_urls") or 0) for x in rows),
        "excluded_external_domain_urls": sum(int(x.get("excluded_external_domain_urls") or 0) for x in rows),
        "excluded_non_page_urls": sum(int(x.get("excluded_non_page_urls") or 0) for x in rows),
        "resolution_candidates": sum(int(x.get("resolution_candidates") or 0) for x in rows),
        "xmls_traversed": sum(int(x.get("xmls_traversed") or 0) for x in rows),
    }
    run.config_json = cfg
    db.commit()


def _store_live_discovery_issues(db, run, rows: list[dict]):
    cfg = dict(run.config_json or {})
    # Discovery-document issues are intentionally small metadata, unlike page
    # result matrices. Keeping them here preserves exact child-sitemap audit
    # evidence even when the user stops the run before final analysis_json.
    cfg["_sitemap_discovery_issues"] = list(rows)
    run.config_json = cfg
    db.commit()


def _set_live_resolution(run, stats: dict):
    cfg = dict(run.config_json or {})
    cfg["_sitemap_resolution"] = dict(stats)
    run.config_json = cfg


def _cache_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def _cache_get(db, url: str):
    row = db.query(models.SitemapPageCache).filter_by(url_hash=_cache_hash(url)).first()
    if not row:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        return None
    payload = dict(row.payload_json or {})
    # Quality version 2 includes challenge/WAF detection and taxonomy exclusion.
    # Older cache rows are recrawled once so historical block pages cannot leak
    # into the improved topic taxonomy.
    if int(payload.get("quality_version") or 0) < 2:
        return None
    return payload


def _cache_put(db, url: str, payload: dict, *, commit: bool = True):
    key = _cache_hash(url)
    now = _now()
    row = db.query(models.SitemapPageCache).filter_by(url_hash=key).first()
    if not row:
        row = models.SitemapPageCache(url_hash=key, normalized_url=normalize_url(url), payload_json={})
        db.add(row)
    row.normalized_url = normalize_url(url)
    cache_payload = dict(payload or {})
    cache_payload["quality_version"] = 2
    row.payload_json = cache_payload
    row.content_hash = payload.get("content_hash")
    row.stored_at = now
    row.expires_at = now + timedelta(hours=settings.sitemap_cache_ttl_hours)
    if commit:
        db.commit()


def _cache_put_many(db, items: list[tuple[str, dict]]):
    if not items:
        return
    for url, payload in items:
        _cache_put(db, url, payload, commit=False)
    db.commit()


_TAXONOMY_CONTENT_TYPES = {
    "PRODUCT", "CONTENT", "CATEGORY", "TOOL_CALCULATOR", "FAQ_HELP", "LANDING_PAGE"
}


def _taxonomy_eligibility(evidence: dict, classification: dict) -> tuple[bool, str]:
    if evidence.get("error"):
        return False, str(evidence.get("terminal_state") or "fetch_error")
    terminal = str(evidence.get("terminal_state") or "usable").lower()
    if terminal != "usable":
        return False, terminal
    page_type = str(classification.get("detailed_page_type") or "OTHER").upper()
    if page_type not in _TAXONOMY_CONTENT_TYPES:
        return False, f"excluded_page_type:{page_type}"
    return True, "eligible"


def _row_payload(source, entry, evidence: dict, classification: dict, topic: dict) -> dict:
    taxonomy_eligible, taxonomy_reason = _taxonomy_eligibility(evidence, classification)
    return {
        "source_id": source.id,
        "root_sitemap": source.root_url,
        "child_sitemap": entry.child_sitemap,
        "original_url": entry.url,
        "normalized_url": normalize_url(evidence.get("canonical_url") or evidence.get("final_url") or entry.url),
        "lastmod": entry.lastmod,
        **evidence,
        "broad_page_type": classification.get("broad_page_type", "OTHER"),
        "detailed_page_type": classification.get("detailed_page_type", "OTHER"),
        "type_confidence": classification.get("confidence", 0),
        "classification_method": classification.get("method", "deterministic"),
        "classification_evidence": {
            **classification,
            "terminal_state": evidence.get("terminal_state"),
            "browser_fallback_attempted": evidence.get("browser_fallback_attempted", False),
            "browser_fallback_status": evidence.get("browser_fallback_status"),
            "recovered_by_browser": evidence.get("recovered_by_browser", False),
            "retryable": evidence.get("retryable"),
            "challenge_detected": evidence.get("challenge_detected", False),
            "challenge_reason": evidence.get("challenge_reason"),
            "taxonomy_eligible": taxonomy_eligible,
            "taxonomy_exclusion_reason": None if taxonomy_eligible else taxonomy_reason,
        },
        "category": topic.get("category", "Other / Unclassified"),
        "topic": topic.get("topic", "Other / Unclassified"),
        "subtopic": topic.get("subtopic", ""),
        "topic_confidence": topic.get("confidence", 0),
        "topic_method": topic.get("method", "deterministic"),
    }


def _to_model(run_id: int, payload: dict) -> models.SitemapUrl:
    return models.SitemapUrl(
        sitemap_run_id=run_id, source_id=payload["source_id"], root_sitemap=payload["root_sitemap"],
        child_sitemap=payload.get("child_sitemap"), original_url=payload["original_url"], normalized_url=payload["normalized_url"],
        final_url=payload.get("final_url"), canonical_url=payload.get("canonical_url"), lastmod=payload.get("lastmod"),
        http_status=payload.get("status_code"), fetch_method=payload.get("fetch_method"), retry_count=int(payload.get("retry_count") or 0),
        broad_page_type=payload.get("broad_page_type", "OTHER"), detailed_page_type=payload.get("detailed_page_type", "OTHER"),
        type_confidence=float(payload.get("type_confidence") or 0), classification_method=payload.get("classification_method", "deterministic"),
        classification_evidence_json=payload.get("classification_evidence") or {}, category=payload.get("category", "Other / Unclassified"),
        topic=payload.get("topic", "Other / Unclassified"), subtopic=payload.get("subtopic") or None,
        topic_confidence=float(payload.get("topic_confidence") or 0), topic_method=payload.get("topic_method", "deterministic"),
        title=payload.get("title"), meta_description=payload.get("meta_description"), h1_json=payload.get("h1") or [], h2_json=payload.get("h2") or [],
        schema_types_json=payload.get("schema_types") or [], breadcrumbs_json=payload.get("breadcrumbs") or [], word_count=int(payload.get("word_count") or 0),
        author_present=bool(payload.get("author_present")), published_at_text=payload.get("published_at_text") or None,
        cta_json=payload.get("cta_texts") or [], text_excerpt=payload.get("text_excerpt"), content_hash=payload.get("content_hash"),
        fetched_at=_now(), error=payload.get("error"),
    )


def _apply_model_payload(row: models.SitemapUrl, payload: dict) -> None:
    """Apply one fully classified payload to an existing persisted row."""
    row.source_id = payload["source_id"]
    row.root_sitemap = payload["root_sitemap"]
    row.child_sitemap = payload.get("child_sitemap")
    row.original_url = payload["original_url"]
    row.normalized_url = payload["normalized_url"]
    row.final_url = payload.get("final_url")
    row.canonical_url = payload.get("canonical_url")
    row.lastmod = payload.get("lastmod")
    row.http_status = payload.get("status_code")
    row.fetch_method = payload.get("fetch_method")
    row.retry_count = int(payload.get("retry_count") or 0)
    row.broad_page_type = payload.get("broad_page_type", "OTHER")
    row.detailed_page_type = payload.get("detailed_page_type", "OTHER")
    row.type_confidence = float(payload.get("type_confidence") or 0)
    row.classification_method = payload.get("classification_method", "deterministic")
    row.classification_evidence_json = payload.get("classification_evidence") or {}
    row.category = payload.get("category", "Other / Unclassified")
    row.topic = payload.get("topic", "Other / Unclassified")
    row.subtopic = payload.get("subtopic") or None
    row.topic_confidence = float(payload.get("topic_confidence") or 0)
    row.topic_method = payload.get("topic_method", "deterministic")
    row.title = payload.get("title")
    row.meta_description = payload.get("meta_description")
    row.h1_json = payload.get("h1") or []
    row.h2_json = payload.get("h2") or []
    row.schema_types_json = payload.get("schema_types") or []
    row.breadcrumbs_json = payload.get("breadcrumbs") or []
    row.word_count = int(payload.get("word_count") or 0)
    row.author_present = bool(payload.get("author_present"))
    row.published_at_text = payload.get("published_at_text") or None
    row.cta_json = payload.get("cta_texts") or []
    row.text_excerpt = payload.get("text_excerpt")
    row.content_hash = payload.get("content_hash")
    row.fetched_at = _now()
    row.error = payload.get("error")


def _classification_for_evidence(evidence: dict) -> dict:
    if not evidence.get("error"):
        return classify_page(evidence)
    return {
        "detailed_page_type": "OTHER",
        "broad_page_type": "OTHER",
        "confidence": 0.0,
        "method": "fetch-error",
        "evidence": [evidence.get("error")],
        "scores": {},
    }


def _checkpoint_payload(source, entry, evidence: dict) -> dict:
    classification = _classification_for_evidence(evidence)
    topic = preliminary_topic(evidence) if not evidence.get("error") else {
        "category": "Other / Unclassified",
        "topic": "Other / Unclassified",
        "subtopic": "",
        "confidence": 0.0,
        "method": "not-classified",
    }
    return _row_payload(source, entry, evidence, classification, topic)


def _persist_pending_inventory(db, run_id: int, unique) -> dict[int, int]:
    """Persist the discovered run inventory before network resolution starts.

    These rows make Stop-and-Save durable: if the worker is cancelled halfway
    through resolution, every discovered URL remains visible and every completed
    resolution checkpoint already contains real evidence.  Pending placeholders
    are explicit warnings, never recoverable page failures.
    """
    batch_size = max(100, int(settings.sitemap_db_batch_size))
    for start in range(0, len(unique), batch_size):
        rows = []
        for source, entry in unique[start:start + batch_size]:
            rows.append(models.SitemapUrl(
                sitemap_run_id=run_id,
                source_id=source.id,
                root_sitemap=source.root_url,
                child_sitemap=entry.child_sitemap,
                original_url=entry.url,
                normalized_url=normalize_url(entry.url),
                lastmod=entry.lastmod,
                broad_page_type="OTHER",
                detailed_page_type="OTHER",
                type_confidence=0.0,
                classification_method="pending",
                classification_evidence_json={
                    "terminal_state": "pending_resolution",
                    "taxonomy_eligible": False,
                    "taxonomy_exclusion_reason": "pending_resolution",
                },
                category="Other / Unclassified",
                topic="Other / Unclassified",
                topic_confidence=0.0,
                topic_method="pending",
                error="Pending page resolution",
            ))
        db.add_all(rows)
        db.commit()

    ids = {
        str(normalized): int(row_id)
        for row_id, normalized in (
            db.query(models.SitemapUrl.id, models.SitemapUrl.normalized_url)
            .filter(models.SitemapUrl.sitemap_run_id == run_id)
            .all()
        )
    }
    return {
        i: ids[normalize_url(entry.url)]
        for i, (_source, entry) in enumerate(unique)
    }


def _checkpoint_evidence_rows(db, unique, row_id_by_index: dict[int, int], items: list[tuple[int, dict]]) -> None:
    if not items:
        return
    ids = [row_id_by_index[i] for i, _payload in items]
    rows = (
        db.query(models.SitemapUrl)
        .filter(models.SitemapUrl.id.in_(ids))
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    for index, evidence in items:
        row = by_id.get(int(row_id_by_index[index]))
        if row is None:
            continue
        source, entry = unique[index]
        payload = _checkpoint_payload(source, entry, evidence)
        # During checkpointing preserve the original normalized URL. Final
        # redirect/canonical collision reconciliation happens after all fallbacks.
        payload["normalized_url"] = normalize_url(entry.url)
        _apply_model_payload(row, payload)
    db.commit()


def _fair_source_resolution_indices(unique, indices: list[int]) -> list[int]:
    """Interleave source batches so one giant competitor cannot dominate ordering."""
    if not indices:
        return []
    batch_size = max(1, int(settings.sitemap_source_round_robin_batch_size))
    by_source: dict[int, list[int]] = defaultdict(list)
    source_order: list[int] = []
    for i in indices:
        source_id = int(getattr(unique[i][0], "id", 0) or 0)
        if source_id not in by_source:
            source_order.append(source_id)
        by_source[source_id].append(i)

    offsets = {source_id: 0 for source_id in source_order}
    ordered: list[int] = []
    while len(ordered) < len(indices):
        advanced = False
        for source_id in source_order:
            rows = by_source[source_id]
            start = offsets[source_id]
            if start >= len(rows):
                continue
            end = min(len(rows), start + batch_size)
            ordered.extend(rows[start:end])
            offsets[source_id] = end
            advanced = True
        if not advanced:
            break
    return ordered


def _resolution_summary(evidence_by_index: dict[int, dict], *, total_candidates: int, cache_hits: int, uncached_total: int) -> dict:
    status_counts: dict[str, int] = defaultdict(int)
    host_rate_events: dict[str, int] = defaultdict(int)
    out = {
        "resolution_candidates": int(total_candidates),
        "cache_hits": int(cache_hits),
        "uncached_total": int(uncached_total),
        "processed_uncached": int(uncached_total),
        "resolved_successfully": 0,
        "resolution_failed": 0,
        "stale_404_410": 0,
        "non_html": 0,
        "security_challenge": 0,
        "host_access_deferred": 0,
        "rate_limited_429": 0,
        "recovered_by_impersonation": 0,
        "recovered_by_browser": 0,
        "recovered_by_retry": 0,
    }
    for payload in evidence_by_index.values():
        status = payload.get("status_code")
        status_counts[str(status)] += 1
        host = str(payload.get("rate_limit_host") or "").strip()
        if host:
            host_rate_events[host] = max(host_rate_events[host], int(payload.get("host_rate_limit_events") or 0))
        if not payload.get("error"):
            out["resolved_successfully"] += 1
            if payload.get("recovered_by_impersonation"):
                out["recovered_by_impersonation"] += 1
            if payload.get("recovered_by_browser"):
                out["recovered_by_browser"] += 1
            if payload.get("recovered_by_retry"):
                out["recovered_by_retry"] += 1
            continue
        out["resolution_failed"] += 1
        if status in {404, 410}:
            out["stale_404_410"] += 1
        elif payload.get("non_html"):
            out["non_html"] += 1
        elif payload.get("terminal_state") == "security_challenge":
            out["security_challenge"] += 1
        elif payload.get("terminal_state") == "host_access_deferred":
            out["host_access_deferred"] += 1
        if status == 429:
            out["rate_limited_429"] += 1
    out["status_counts"] = dict(sorted(status_counts.items()))
    out["rate_limit_events"] = sum(host_rate_events.values())
    out["rate_limited_hosts"] = [
        {"host": host, "events": events}
        for host, events in sorted(host_rate_events.items(), key=lambda x: (-x[1], x[0]))[:25]
        if events > 0
    ]
    return out


async def execute_sitemap_run(run_id: int):
    db = SessionLocal()
    try:
        run = db.get(models.SitemapRun, run_id)
        if not run:
            return
        if run.status in {"completed", "completed_with_warnings", "partial", "cancelled", "cancelling"}:
            return
        cfg = dict(run.config_json or {})
        if cfg.get("_recovery_only"):
            from app.services.sitemap_recovery import recover_sitemap_run
            await recover_sitemap_run(run_id, db=db)
            return
        run.status = "running"; run.started_at = _now(); run.error = None
        db.execute(delete(models.SitemapOpportunity).where(models.SitemapOpportunity.sitemap_run_id == run_id))
        db.execute(delete(models.SitemapDuplicate).where(models.SitemapDuplicate.sitemap_run_id == run_id))
        db.execute(delete(models.SitemapUrl).where(models.SitemapUrl.sitemap_run_id == run_id))
        cfg = dict(run.config_json or {})
        cfg["_duplicate_audit"] = {
            "version": 1,
            "mode": "native",
            "started_at": _now().isoformat(),
        }
        run.config_json = cfg
        db.commit()
        cfg = dict(run.config_json or {})
        enable_ai = bool(cfg.get("enable_ai", True)) and settings.ai_mode != "off"
        use_cache = bool(cfg.get("use_cache", True)) and not bool(cfg.get("force_refresh", False))
        max_urls = int(cfg.get("max_urls_per_source") or settings.sitemap_max_urls_per_source)
        resolution_scope = str(cfg.get("resolution_scope") or settings.sitemap_resolution_scope).strip().lower()
        exclude_non_pages_cfg = cfg.get("exclude_non_page_urls")
        exclude_non_pages = (
            settings.sitemap_exclude_obvious_non_pages
            if exclude_non_pages_cfg is None
            else bool(exclude_non_pages_cfg)
        )
        resolve_concurrency = int(cfg.get("resolve_concurrency") or settings.sitemap_resolve_concurrency)
        playwright_concurrency = int(cfg.get("playwright_concurrency") or settings.sitemap_playwright_concurrency)

        source_snapshot = list(
            dict(cfg.get("_input_snapshot") or {}).get("sources") or []
        )
        if source_snapshot:
            sources = [
                SimpleNamespace(**item)
                for item in source_snapshot
                if item.get("id") is not None
            ]
        else:
            # Backward compatibility for runs created before immutable snapshots.
            sources = (
                db.query(models.SitemapSource)
                .filter_by(project_id=run.project_id, enabled=True)
                .order_by(models.SitemapSource.id)
                .all()
            )

        _progress(db, run, "sitemap_discovery", 0, 10, 0, "Discovering complete recursive sitemap inventory", 0, len(sources))

        discovered: list[tuple[models.SitemapSource, object]] = []
        discovery_failures: list[dict] = []
        source_discovery: list[dict] = []
        for idx, source in enumerate(sources, start=1):
            entries, issues, discovery_stats = await discover_sitemap_inventory(
                source.root_url,
                max_urls=max_urls,
            )
            duplicate_evidence = list(discovery_stats.pop("duplicate_evidence", []) or [])
            add_discovery_duplicates(
                db,
                run_id,
                source,
                duplicate_evidence,
                audit_stage="sitemap_discovery",
            )
            selected_entries, scope_stats = select_resolution_entries(
                entries,
                scope_mode=resolution_scope,
                exclude_non_pages=exclude_non_pages,
            )
            discovered.extend((source, entry) for entry in selected_entries)

            errors = [x for x in issues if (x.get("severity") or "error") == "error"]
            warnings = [x for x in issues if (x.get("severity") or "error") != "error"]
            discovery_status = _discovery_status(entries, errors, warnings, discovery_stats)

            message_parts = []
            if errors:
                message_parts.append(f"{len(errors)} discovery error(s)")
            if warnings:
                message_parts.append(f"{len(warnings)} warning(s)")
            if scope_stats["excluded_by_scope_urls"]:
                message_parts.append(f"{scope_stats['excluded_by_scope_urls']:,} excluded by {resolution_scope} scope")
            if scope_stats["excluded_non_page_urls"]:
                message_parts.append(f"{scope_stats['excluded_non_page_urls']:,} obvious non-page URL(s) excluded")
            message = ", ".join(message_parts) or "Discovery successful"

            source_row = {
                "source_id": source.id,
                "site": source.label,
                "role": source.role,
                "root_sitemap": source.root_url,
                "status": discovery_status,
                # Explicit funnel. `discovered_urls` remains a backward-compatible
                # alias for the URLs actually entering page resolution.
                "raw_url_records": int(discovery_stats.get("raw_url_records") or 0),
                "unique_sitemap_urls": int(discovery_stats.get("unique_sitemap_urls") or len(entries)),
                "duplicate_url_records": int(discovery_stats.get("duplicate_url_records") or 0),
                "xmls_traversed": int(discovery_stats.get("xmls_traversed") or 0),
                "max_depth": int(discovery_stats.get("max_depth") or 0),
                "failed_xmls": int(discovery_stats.get("failed_xmls") or 0),
                "empty_child_sitemaps": int(discovery_stats.get("empty_child_sitemaps") or 0),
                "root_document_parsed": bool(discovery_stats.get("root_document_parsed")),
                "root_host_urls": int(scope_stats.get("root_host_urls") or 0),
                "related_subdomain_urls": int(scope_stats.get("related_subdomain_urls") or 0),
                "external_domain_urls": int(scope_stats.get("external_domain_urls") or 0),
                "in_scope_urls": int(scope_stats.get("in_scope_urls") or 0),
                "excluded_by_scope_urls": int(scope_stats.get("excluded_by_scope_urls") or 0),
                "excluded_external_domain_urls": int(scope_stats.get("excluded_external_domain_urls") or 0),
                "excluded_non_page_urls": int(scope_stats.get("excluded_non_page_urls") or 0),
                "resolution_candidates": int(scope_stats.get("resolution_candidates") or 0),
                "discovered_urls": int(scope_stats.get("resolution_candidates") or 0),
                "scope_mode": resolution_scope,
                "external_domain_samples": scope_stats.get("external_domain_samples") or [],
                "non_page_samples": scope_stats.get("non_page_samples") or [],
                "errors": len(errors),
                "warnings": len(warnings),
                "message": message,
            }
            source_discovery.append(source_row)
            _store_live_discovery(db, run, source_discovery)

            for issue in issues:
                discovery_failures.append({
                    "source_id": source.id,
                    "site": source.label,
                    "role": source.role,
                    "root_sitemap": source.root_url,
                    "stage": "sitemap_discovery",
                    "url": issue.get("sitemap") or source.root_url,
                    "parent_sitemap": issue.get("parent_sitemap"),
                    "final_sitemap": issue.get("final_sitemap"),
                    "http_status": issue.get("http_status"),
                    "fetch_method": issue.get("fetch_method"),
                    "retry_count": int(issue.get("retry_count") or 0),
                    "verification_attempts": int(issue.get("verification_attempts") or 0),
                    "empty_kind": issue.get("empty_kind"),
                    "severity": issue.get("severity") or "error",
                    "code": issue.get("code") or "SITEMAP_DISCOVERY_ISSUE",
                    "error": issue.get("error") or "Unknown sitemap discovery issue",
                })
            _store_live_discovery_issues(db, run, discovery_failures)
            _progress(
                db, run, "sitemap_discovery", 0, 10, idx/max(len(sources),1),
                (
                    f"{source.label}: {source_row['unique_sitemap_urls']:,} unique sitemap URLs; "
                    f"{source_row['resolution_candidates']:,} selected for resolution"
                ),
                idx, len(sources),
            )

        # Deduplicate by normalized URL across the ENTIRE run.
        # The database uniqueness constraint is (sitemap_run_id, normalized_url),
        # so source_id must NOT be part of this key. Keeping source_id here can
        # allow the same URL to survive under two sources and fail during insert.
        unique = []
        seen: dict[str, tuple[object, object]] = {}
        for source, entry in discovered:
            key = normalize_url(entry.url)
            first = seen.get(key)
            if first is not None:
                first_source, first_entry = first
                add_cross_source_duplicate(
                    db,
                    run_id,
                    first_source,
                    first_entry,
                    source,
                    entry,
                    audit_stage="run_dedup",
                )
                continue
            seen[key] = (source, entry)
            unique.append((source, entry))
        db.commit()
        if not unique:
            raise RuntimeError("No sitemap URLs were discovered")

        # Persist the discovered inventory before page resolution. This is the
        # durable Stop-and-Save boundary for long runs.
        row_id_by_index = _persist_pending_inventory(db, run_id, unique)
        run.progress_total = _PROGRESS_SCALE; db.commit()

        cached_payloads: dict[int, dict] = {}
        to_resolve_indices = []
        for i, (source, entry) in enumerate(unique):
            cached = _cache_get(db, entry.url) if use_cache else None
            if cached:
                cached_payloads[i] = cached
            else:
                to_resolve_indices.append(i)

        scheduled_resolve_indices = _fair_source_resolution_indices(unique, to_resolve_indices)
        if cached_payloads:
            _checkpoint_evidence_rows(
                db, unique, row_id_by_index, list(cached_payloads.items())
            )
        total_candidates = len(unique)
        cache_hits = len(cached_payloads)
        initial_fraction = cache_hits / max(total_candidates, 1)
        _progress(
            db, run, "page_resolution", 10, 48, initial_fraction,
            f"Resolution candidates {total_candidates:,}; {cache_hits:,} cache hits; {len(to_resolve_indices):,} uncached pages",
            cache_hits, total_candidates,
        )

        live_resolution = {
            "resolution_candidates": total_candidates,
            "cache_hits": cache_hits,
            "uncached_total": len(to_resolve_indices),
            "processed_uncached": 0,
            "resolved_successfully": cache_hits,
            "resolution_failed": 0,
            "stale_404_410": 0,
            "non_html": 0,
            "security_challenge": 0,
            "host_access_deferred": 0,
            "rate_limited_429": 0,
        }
        _set_live_resolution(run, live_resolution)
        db.commit()

        last_progress_written = cache_hits
        progress_every = max(1, int(settings.sitemap_progress_update_every))
        checkpoint_every = max(1, int(settings.sitemap_checkpoint_every))
        checkpoint_buffer: list[tuple[str, dict]] = []
        resolution_row_buffer: list[tuple[int, dict]] = []
        checkpoint_lock = asyncio.Lock()

        async def resolver_progress(done, total, message):
            nonlocal last_progress_written
            overall_done = cache_hits + done
            if (
                overall_done >= total_candidates
                or overall_done - last_progress_written >= progress_every
            ):
                _set_live_resolution(run, live_resolution)
                _progress(
                    db, run, "page_resolution", 10, 48,
                    overall_done / max(total_candidates, 1),
                    f"Resolving pages ({overall_done:,}/{total_candidates:,}); {cache_hits:,} served from cache",
                    overall_done, total_candidates,
                )
                last_progress_written = overall_done

        async def checkpoint_result(local_index: int, payload: dict):
            live_resolution["processed_uncached"] += 1
            if not payload.get("error"):
                live_resolution["resolved_successfully"] += 1
            else:
                live_resolution["resolution_failed"] += 1
                if payload.get("status_code") == 429:
                    live_resolution["rate_limited_429"] += 1
                if payload.get("status_code") in {404, 410}:
                    live_resolution["stale_404_410"] += 1
                elif payload.get("non_html"):
                    live_resolution["non_html"] += 1
                elif payload.get("terminal_state") == "security_challenge":
                    live_resolution["security_challenge"] += 1
                elif payload.get("terminal_state") == "host_access_deferred":
                    live_resolution["host_access_deferred"] += 1

            global_index = scheduled_resolve_indices[local_index]
            terminal = str(payload.get("terminal_state") or "")
            cacheable_terminal = terminal in {"stale_sitemap_url", "non_html"}

            async with checkpoint_lock:
                resolution_row_buffer.append((global_index, payload))
                if use_cache and ((not payload.get("error")) or cacheable_terminal):
                    checkpoint_buffer.append((unique[global_index][1].url, payload))

                if len(resolution_row_buffer) >= checkpoint_every:
                    batch_rows = list(resolution_row_buffer)
                    resolution_row_buffer.clear()
                    _checkpoint_evidence_rows(db, unique, row_id_by_index, batch_rows)

                if len(checkpoint_buffer) >= checkpoint_every:
                    batch_cache = list(checkpoint_buffer)
                    checkpoint_buffer.clear()
                    _cache_put_many(db, batch_cache)

        resolved = await resolve_pages_http(
            [unique[i][1].url for i in scheduled_resolve_indices],
            progress=resolver_progress,
            result_callback=checkpoint_result,
            concurrency=resolve_concurrency,
        )
        if resolution_row_buffer:
            _checkpoint_evidence_rows(db, unique, row_id_by_index, list(resolution_row_buffer))
            resolution_row_buffer.clear()
        if checkpoint_buffer:
            _cache_put_many(db, list(checkpoint_buffer))
            checkpoint_buffer.clear()
        _set_live_resolution(run, live_resolution)
        db.commit()

        evidence_by_index = dict(cached_payloads)
        for i, payload in zip(scheduled_resolve_indices, resolved):
            evidence_by_index[i] = payload

        _progress(
            db, run, "page_resolution", 10, 48, 1,
            f"Page resolution complete for {total_candidates:,} candidates ({cache_hits:,} cache hits)",
            total_candidates, total_candidates,
        )

        # First deterministic classification.
        classifications: dict[int, dict] = {}
        for i in range(len(unique)):
            evidence = evidence_by_index[i]
            classifications[i] = (
                classify_page(evidence)
                if not evidence.get("error")
                else {
                    "detailed_page_type": "OTHER",
                    "broad_page_type": "OTHER",
                    "confidence": 0.0,
                    "method": "fetch-error",
                    "evidence": [evidence.get("error")],
                    "scores": {},
                }
            )

        # --------------------------------------------------------------
        # Lightweight browser-fingerprint HTTP fallback (curl-cffi)
        # --------------------------------------------------------------
        all_impersonation_indices = [
            i for i in range(len(unique))
            if should_impersonation_fallback(evidence_by_index[i])
        ]

        # Genuine per-URL 401/403/406 responses still go through curl-cffi. URLs
        # that were never launched because the host-level access circuit opened are
        # NOT equivalent to 100k independent 403s: probe only a small per-host sample
        # here and preserve the rest as recoverable/deferred for the safe recovery
        # path. This prevents a second fallback storm immediately after the first one.
        hard_impersonation_indices = []
        deferred_impersonation_indices = []
        soft_impersonation_indices = []
        for i in all_impersonation_indices:
            evidence = evidence_by_index[i]
            status = evidence.get("status_code")
            terminal = str(evidence.get("terminal_state") or "").lower()
            if terminal == "host_access_deferred" or evidence.get("host_access_deferred"):
                deferred_impersonation_indices.append(i)
            elif (
                status in {401, 403, 406}
                or terminal == "security_challenge"
                or evidence.get("challenge_detected")
            ):
                hard_impersonation_indices.append(i)
            else:
                soft_impersonation_indices.append(i)

        deferred_probe_cap = max(0, int(settings.sitemap_host_access_probe_per_host))
        deferred_probe_indices = []
        deferred_probe_counts: dict[str, int] = defaultdict(int)
        for i in deferred_impersonation_indices:
            evidence = evidence_by_index[i]
            url = str(evidence.get("final_url") or evidence.get("requested_url") or unique[i][1].url)
            host = (urlparse(url).hostname or "").lower() or "unknown"
            if deferred_probe_counts[host] >= deferred_probe_cap:
                continue
            deferred_probe_counts[host] += 1
            deferred_probe_indices.append(i)

        soft_impersonation_indices.sort(
            key=lambda i: (
                0 if evidence_by_index[i].get("status_code") == 429 else 1,
                i,
            )
        )
        impersonation_cap = max(0, int(settings.sitemap_impersonation_fallback_max_pages))
        if impersonation_cap:
            remaining_soft_slots = max(
                0,
                impersonation_cap - len(hard_impersonation_indices) - len(deferred_probe_indices),
            )
            soft_impersonation_indices = soft_impersonation_indices[:remaining_soft_slots]
        else:
            soft_impersonation_indices = []

        impersonation_indices = (
            hard_impersonation_indices
            + deferred_probe_indices
            + soft_impersonation_indices
        )
        impersonation_candidates = [
            {"item_id": i, **evidence_by_index[i], "classification": classifications[i]}
            for i in impersonation_indices
        ]
        live_resolution["impersonation_candidates"] = len(impersonation_candidates)
        live_resolution["impersonation_recovered"] = 0
        live_resolution["host_access_deferred_candidates"] = len(deferred_impersonation_indices)
        live_resolution["host_access_probe_candidates"] = len(deferred_probe_indices)
        _set_live_resolution(run, live_resolution)
        db.commit()

        _progress(
            db, run, "impersonated_http_fallback", 48, 54,
            0 if impersonation_candidates else 1,
            (
                f"Lightweight browser-fingerprint fallback for {len(impersonation_candidates):,} "
                "WAF/403/transport failures"
                if impersonation_candidates
                else "No lightweight browser-fingerprint fallback required"
            ),
            0 if impersonation_candidates else len(impersonation_candidates),
            len(impersonation_candidates),
        )

        impersonation_buffer: list[tuple[str, dict]] = []
        impersonation_lock = asyncio.Lock()
        last_impersonation_progress = 0

        async def impersonation_checkpoint(local_index: int, payload: dict):
            nonlocal last_impersonation_progress
            original = impersonation_candidates[local_index]
            global_index = original["item_id"]
            evidence_by_index[global_index] = payload
            classifications[global_index] = (
                classify_page(payload)
                if not payload.get("error")
                else {
                    "detailed_page_type": "OTHER",
                    "broad_page_type": "OTHER",
                    "confidence": 0.0,
                    "method": "fetch-error",
                    "evidence": [payload.get("error")],
                    "scores": {},
                }
            )
            if not payload.get("error"):
                live_resolution["impersonation_recovered"] += 1

            terminal = str(payload.get("terminal_state") or "")
            cacheable = (not payload.get("error")) or terminal in {"stale_sitemap_url", "non_html"}
            if use_cache and cacheable:
                url = unique[global_index][1].url
                async with impersonation_lock:
                    impersonation_buffer.append((url, payload))
                    if len(impersonation_buffer) >= checkpoint_every:
                        batch = list(impersonation_buffer)
                        impersonation_buffer.clear()
                        _cache_put_many(db, batch)

        async def impersonation_progress(done, total, message):
            nonlocal last_impersonation_progress
            if done >= total or done - last_impersonation_progress >= progress_every:
                _set_live_resolution(run, live_resolution)
                _progress(
                    db, run, "impersonated_http_fallback", 48, 54,
                    done / max(total, 1), message, done, total,
                )
                last_impersonation_progress = done

        if impersonation_candidates:
            impersonated = await resolve_pages_impersonated(
                impersonation_candidates,
                progress=impersonation_progress,
                result_callback=impersonation_checkpoint,
                concurrency=settings.sitemap_impersonation_concurrency,
            )
            # Defensive final assignment in case a callback was bypassed by an
            # implementation-level exception.
            for original, payload in zip(impersonation_candidates, impersonated):
                i = original["item_id"]
                evidence_by_index[i] = payload
                classifications[i] = (
                    classify_page(payload)
                    if not payload.get("error")
                    else {
                        "detailed_page_type": "OTHER",
                        "broad_page_type": "OTHER",
                        "confidence": 0.0,
                        "method": "fetch-error",
                        "evidence": [payload.get("error")],
                        "scores": {},
                    }
                )
        if impersonation_buffer:
            _cache_put_many(db, list(impersonation_buffer))
            impersonation_buffer.clear()
        if impersonation_indices:
            _checkpoint_evidence_rows(
                db, unique, row_id_by_index,
                [(i, evidence_by_index[i]) for i in impersonation_indices],
            )
        _set_live_resolution(run, live_resolution)
        db.commit()
        _progress(
            db, run, "impersonated_http_fallback", 48, 54, 1,
            f"Lightweight fallback complete; {live_resolution.get('impersonation_recovered', 0):,} recovered",
            len(impersonation_candidates), len(impersonation_candidates),
        )

        # --------------------------------------------------------------
        # Status-aware, family-sampled Chromium fallback
        # --------------------------------------------------------------
        raw_browser_candidates = []
        for i in range(len(unique)):
            evidence = evidence_by_index[i]
            cls = classifications[i]
            if needs_browser_fallback(evidence, cls) and evidence.get("fetch_method") != "playwright":
                raw_browser_candidates.append({
                    "item_id": i,
                    **evidence,
                    "classification": cls,
                })

        browser_candidates, browser_selection = select_browser_fallback_candidates(
            raw_browser_candidates,
            max_pages=settings.sitemap_browser_fallback_max_pages,
            per_host_cap=settings.sitemap_browser_fallback_per_host_cap,
            per_family_cap=settings.sitemap_browser_family_sample_size,
            stale_cap=settings.sitemap_browser_stale_sample_max,
        )
        live_resolution["browser_fallback"] = browser_selection
        _set_live_resolution(run, live_resolution)
        db.commit()

        _progress(
            db, run, "browser_fallback", 54, 62,
            0 if browser_candidates else 1,
            (
                f"Chromium last-resort fallback: {len(browser_candidates):,} selected from "
                f"{len(raw_browser_candidates):,} candidates after status/family/host caps"
                if browser_candidates
                else "Chromium fallback skipped: no browser-helpful candidates"
            ),
            0 if browser_candidates else len(browser_candidates),
            len(browser_candidates),
        )

        browser_checkpoint_every = max(1, int(settings.sitemap_browser_checkpoint_every))
        browser_buffer: list[tuple[str, dict]] = []
        browser_lock = asyncio.Lock()
        last_browser_progress = 0

        async def browser_checkpoint(local_index: int, payload: dict):
            original = browser_candidates[local_index]
            global_index = original["item_id"]
            evidence_by_index[global_index] = payload
            classifications[global_index] = (
                classify_page(payload)
                if not payload.get("error")
                else {
                    "detailed_page_type": "OTHER",
                    "broad_page_type": "OTHER",
                    "confidence": 0.0,
                    "method": "fetch-error",
                    "evidence": [payload.get("error")],
                    "scores": {},
                }
            )
            if payload.get("browser_fallback_error"):
                classifications[global_index]["browser_fallback_status"] = payload.get("browser_fallback_status")
                classifications[global_index]["browser_fallback_warning"] = payload.get("browser_fallback_error")

            terminal = str(payload.get("terminal_state") or "")
            cacheable = (not payload.get("error")) or terminal in {"stale_sitemap_url", "non_html"}
            if use_cache and cacheable:
                url = unique[global_index][1].url
                async with browser_lock:
                    browser_buffer.append((url, payload))
                    if len(browser_buffer) >= browser_checkpoint_every:
                        batch = list(browser_buffer)
                        browser_buffer.clear()
                        _cache_put_many(db, batch)

        async def browser_progress(done, total, message):
            nonlocal last_browser_progress
            if done >= total or done - last_browser_progress >= max(5, min(progress_every, 25)):
                _progress(
                    db, run, "browser_fallback", 54, 62,
                    done / max(total, 1), message, done, total,
                )
                last_browser_progress = done

        if browser_candidates:
            rendered = await render_pages_browser(
                browser_candidates,
                progress=browser_progress,
                result_callback=browser_checkpoint,
                concurrency=playwright_concurrency,
            )
            for original, payload in zip(browser_candidates, rendered):
                i = original["item_id"]
                evidence_by_index[i] = payload
                classifications[i] = (
                    classify_page(payload)
                    if not payload.get("error")
                    else {
                        "detailed_page_type": "OTHER",
                        "broad_page_type": "OTHER",
                        "confidence": 0.0,
                        "method": "fetch-error",
                        "evidence": [payload.get("error")],
                        "scores": {},
                    }
                )
        if browser_buffer:
            _cache_put_many(db, list(browser_buffer))
            browser_buffer.clear()
        if browser_candidates:
            _checkpoint_evidence_rows(
                db, unique, row_id_by_index,
                [(int(item["item_id"]), evidence_by_index[int(item["item_id"])]) for item in browser_candidates],
            )

        # Page-failure recovery is intentionally NOT performed inline here.
        # The report-producing main analysis continues immediately; persisted
        # recoverable failures are scheduled for automatic background recovery.

        # Reconcile counters from the FINAL evidence map. Earlier counters describe
        # the initial HTTP pass and would otherwise continue to report failures
        # that curl-cffi/browser/final-retry already recovered.
        final_resolution = _resolution_summary(
            evidence_by_index,
            total_candidates=total_candidates,
            cache_hits=cache_hits,
            uncached_total=len(to_resolve_indices),
        )
        for key in ("impersonation_candidates", "browser_fallback"):
            if key in live_resolution:
                final_resolution[key] = live_resolution[key]
        live_resolution = final_resolution
        _set_live_resolution(run, live_resolution)
        db.commit()

        # AI model sees only remaining low-confidence successful pages, in bounded batches.
        low_conf = []
        if enable_ai:
            for i in range(len(unique)):
                if not evidence_by_index[i].get("error") and needs_ai_review(classifications[i]):
                    low_conf.append({"item_id": i, **evidence_by_index[i], "classification": classifications[i]})
            low_conf = low_conf[: settings.sitemap_ai_max_page_classifications]
        _progress(db, run, "page_classification", 62, 70, 0, f"Classifying resolved pages; {len(low_conf):,} require AI-model review")
        ai_classes = classify_ambiguous_pages_ai(low_conf) if low_conf else {}
        for i, ai_row in ai_classes.items():
            ptype = ai_row.get("page_type", "OTHER")
            classifications[i] = {
                "detailed_page_type": ptype, "broad_page_type": broad_page_type(ptype),
                "confidence": round(float(ai_row.get("confidence") or 0), 3), "method": "ai-model",
                "evidence": ai_row.get("reason_codes") or [], "scores": classifications[i].get("scores", {}),
            }

        # Build one candidate row per discovered URL first, then collapse any
        # redirect/canonical collisions by the SAME normalized URL used by the DB
        # uniqueness constraint. This applies to successful AND failed/challenge
        # rows; the old code only deduped successful rows and used source_id in the
        # key, which could still violate (sitemap_run_id, normalized_url).
        candidate_rows: list[tuple[object, dict, dict]] = []
        reconciliation = defaultdict(lambda: {
            "discovered_urls": 0, "usable_urls": 0, "stale_urls": 0,
            "non_html_urls": 0, "security_challenge_urls": 0,
            "failed_urls": 0, "canonical_duplicates": 0,
        })
        for source, _entry in unique:
            reconciliation[source.id]["discovered_urls"] += 1

        for i, (source, entry) in enumerate(unique):
            evidence = evidence_by_index[i]
            topic = preliminary_topic(evidence) if not evidence.get("error") else {
                "category": "Other / Unclassified", "topic": "Other / Unclassified",
                "subtopic": "", "confidence": 0.0, "method": "not-classified",
            }
            payload = _row_payload(source, entry, evidence, classifications[i], topic)
            payload["_cache_source_url"] = entry.url
            candidate_rows.append((source, payload, evidence))

        def _row_quality(payload: dict, evidence: dict) -> tuple:
            classification_evidence = payload.get("classification_evidence") or {}
            return (
                1 if not evidence.get("error") else 0,
                1 if classification_evidence.get("taxonomy_eligible") is True else 0,
                1 if int(evidence.get("status_code") or 0) == 200 else 0,
                float(payload.get("type_confidence") or 0.0),
                int(payload.get("word_count") or 0),
                -int(evidence.get("retry_count") or 0),
            )

        selected_by_url: dict[str, tuple[object, dict, dict]] = {}
        for source, payload, evidence in candidate_rows:
            resolved_key = normalize_url(payload.get("normalized_url") or payload.get("original_url") or "")
            payload["normalized_url"] = resolved_key
            previous = selected_by_url.get(resolved_key)
            if previous is None:
                selected_by_url[resolved_key] = (source, payload, evidence)
                continue

            prev_source, prev_payload, prev_evidence = previous
            if _row_quality(payload, evidence) > _row_quality(prev_payload, prev_evidence):
                reconciliation[prev_source.id]["canonical_duplicates"] += 1
                add_resolved_collision(
                    db,
                    run_id,
                    source,
                    payload,
                    prev_source,
                    prev_payload,
                    resolved_key,
                    audit_stage="resolved_collision",
                )
                selected_by_url[resolved_key] = (source, payload, evidence)
            else:
                reconciliation[source.id]["canonical_duplicates"] += 1
                add_resolved_collision(
                    db,
                    run_id,
                    prev_source,
                    prev_payload,
                    source,
                    payload,
                    resolved_key,
                    audit_stage="resolved_collision",
                )

        db.commit()
        work_rows: list[dict] = []
        for source, payload, evidence in selected_by_url.values():
            if not evidence.get("error"):
                reconciliation[source.id]["usable_urls"] += 1
            elif evidence.get("status_code") in {404, 410}:
                reconciliation[source.id]["stale_urls"] += 1
            elif evidence.get("terminal_state") == "security_challenge":
                reconciliation[source.id]["security_challenge_urls"] += 1
            elif evidence.get("non_html"):
                reconciliation[source.id]["non_html_urls"] += 1
            else:
                reconciliation[source.id]["failed_urls"] += 1
            work_rows.append(payload)

        # Attach terminal-state reconciliation to each source summary before the
        # aggregate/site-profile analysis is built.
        for item in source_discovery:
            stats = reconciliation.get(int(item.get("source_id") or 0))
            if stats:
                item.update(stats)
                accounted = (
                    stats["usable_urls"] + stats["stale_urls"] + stats["non_html_urls"]
                    + stats["security_challenge_urls"] + stats["failed_urls"]
                    + stats["canonical_duplicates"]
                )
                item["accounted_urls"] = accounted
                item["unaccounted_urls"] = max(0, stats["discovered_urls"] - accounted)
        _store_live_discovery(db, run, source_discovery)
        _progress(db, run, "topic_classification", 70, 80, 0.25, "Building a dynamic cross-site topic taxonomy")
        taxonomy_input = [
            x for x in work_rows
            if (x.get("classification_evidence") or {}).get("taxonomy_eligible") is True
        ]
        taxonomy_clusters = build_dynamic_topic_taxonomy(db, taxonomy_input, enable_ai=enable_ai)
        _progress(db, run, "topic_classification", 70, 80, 1, f"Dynamic topic taxonomy complete ({len(taxonomy_clusters):,} clusters)")

        # Cache final page evidence/classification/topic in bounded batches. The
        # resolution-stage checkpoints protect long jobs from losing completed work;
        # this pass upgrades cache rows with final taxonomy/classification data.
        if use_cache:
            final_cache_batch: list[tuple[str, dict]] = []
            for payload in work_rows:
                if payload.get("error"):
                    continue
                cache_source_url = payload.get("_cache_source_url") or payload.get("original_url")
                cache_payload = {
                    k: v for k, v in payload.items()
                    if k not in {
                        "source_id", "root_sitemap", "child_sitemap", "original_url",
                        "normalized_url", "lastmod", "_cache_source_url",
                    }
                }
                final_cache_batch.append((cache_source_url, cache_payload))
                if len(final_cache_batch) >= checkpoint_every:
                    _cache_put_many(db, final_cache_batch)
                    final_cache_batch.clear()
            if final_cache_batch:
                _cache_put_many(db, final_cache_batch)

        for payload in work_rows:
            payload.pop("_cache_source_url", None)

        # Final defensive dedupe immediately before persistence. Pending
        # checkpoint rows already exist; delete collision losers first, then
        # upgrade winners in-place so the uniqueness constraint cannot conflict.
        persist_rows: list[dict] = []
        persist_seen: set[str] = set()
        for payload in work_rows:
            key = normalize_url(payload.get("normalized_url") or payload.get("original_url") or "")
            if not key or key in persist_seen:
                continue
            persist_seen.add(key)
            payload["normalized_url"] = key
            persist_rows.append(payload)

        winner_original_keys = {
            normalize_url(payload.get("original_url") or "")
            for payload in persist_rows
        }
        checkpoint_rows = (
            db.query(models.SitemapUrl)
            .filter(models.SitemapUrl.sitemap_run_id == run_id)
            .all()
        )
        loser_ids = [
            row.id for row in checkpoint_rows
            if normalize_url(row.original_url or "") not in winner_original_keys
        ]
        if loser_ids:
            for start_idx in range(0, len(loser_ids), 500):
                chunk = loser_ids[start_idx:start_idx + 500]
                db.query(models.SitemapUrl).filter(models.SitemapUrl.id.in_(chunk)).delete(
                    synchronize_session=False
                )
                db.commit()

        existing = {
            normalize_url(row.original_url or ""): row
            for row in (
                db.query(models.SitemapUrl)
                .filter(models.SitemapUrl.sitemap_run_id == run_id)
                .all()
            )
        }
        db_batch_size = max(100, int(settings.sitemap_db_batch_size))
        for start_idx in range(0, len(persist_rows), db_batch_size):
            batch = persist_rows[start_idx:start_idx + db_batch_size]
            for payload in batch:
                row = existing.get(normalize_url(payload.get("original_url") or ""))
                if row is None:
                    row = _to_model(run_id, payload)
                    db.add(row)
                else:
                    _apply_model_payload(row, payload)
            db.commit()
        _progress(db, run, "coverage_analysis", 80, 92, 0.3, "Building site, page-type and topic coverage matrices")
        persisted = db.query(models.SitemapUrl).filter_by(sitemap_run_id=run_id).all()
        analysis, opportunities, evidence_index = build_sitemap_analysis(
            persisted, sources, source_discovery=source_discovery
        )
        analysis["dynamic_topic_clusters"] = taxonomy_clusters
        if discovery_failures:
            analysis["sitemap_failures"] = discovery_failures
            analysis["failure_summary"] = (analysis.get("failure_summary") or []) + [
                {
                    "Site": x.get("site"),
                    "Stage": x.get("stage"),
                    "URL": x.get("url"),
                    "Error": x.get("error"),
                    "HTTP Status": x.get("http_status"),
                    "Severity": x.get("severity"),
                }
                for x in discovery_failures
            ]
        comparison_ready = bool((analysis.get("comparison_status") or {}).get("ready"))
        if comparison_ready:
            coverage_message = f"Built {len(opportunities):,} deterministic gap candidates"
        else:
            coverage_message = "Inventory completed; head-to-head opportunities withheld because comparison coverage is incomplete"
        _progress(db, run, "coverage_analysis", 80, 92, 1, coverage_message)

        ai_reviews = {}
        ai_candidates = [
            x for x in opportunities
            if str(x.get("priority") or "").upper() in {"HIGH", "MEDIUM"}
        ]
        if enable_ai and ai_candidates and comparison_ready:
            _progress(
                db, run, "ai_opportunity_review", 92, 98, 0.1,
                f"AI model is reviewing {len(ai_candidates):,} high/medium-value sitemap gaps",
            )
            ai_reviews = review_sitemap_opportunities_ai(ai_candidates, evidence_index)
            _progress(db, run, "ai_opportunity_review", 92, 98, 1, "AI-model opportunity review complete")
        elif enable_ai and comparison_ready:
            _progress(
                db, run, "ai_opportunity_review", 92, 98, 1,
                "AI review skipped: no high/medium sitemap gaps required generative reasoning",
            )

        final_opportunities = []
        for opp in opportunities:
            review = ai_reviews.get(opp["opportunity_key"])
            if review:
                decision = str(review.get("decision") or "REVIEW").upper()
                opp["ai_decision"] = decision
                opp["business_relevance"] = review.get("business_relevance") or opp["business_relevance"]
                # AI confidence is preserved separately; deterministic evidence strength remains authoritative.
                opp["ai_review_confidence"] = float(review.get("confidence") or 0)
                recommended = review.get("recommended_page_type")
                if recommended and recommended != "NO_CHANGE":
                    opp["recommended_page_type"] = recommended
                if review.get("recommendation"):
                    opp["recommendation"] = review["recommendation"]
                opp["evidence_ids"] = list(dict.fromkeys((opp.get("evidence_ids") or []) + (review.get("evidence_ids") or [])))
                opp["source"] = "deterministic+ai-reviewed"
                opp["supporting_subtopics"] = review.get("supporting_subtopics") or []
                if decision == "NO_CHANGE":
                    continue
            final_opportunities.append(opp)
            db.add(models.SitemapOpportunity(
                sitemap_run_id=run_id, opportunity_key=opp["opportunity_key"], gap_type=opp["gap_type"], priority=opp["priority"],
                confidence=float(opp["confidence"]), category=opp.get("category"), topic=opp.get("topic"), recommended_page_type=opp.get("recommended_page_type"),
                client_count=int(opp.get("client_count") or 0), competitor_count=int(opp.get("competitor_count") or 0), competitor_sites=int(opp.get("competitor_sites") or 0),
                business_relevance=opp.get("business_relevance") or "uncertain", title=opp["title"], recommendation=opp["recommendation"],
                evidence_json=opp.get("evidence_ids") or [], source=opp.get("source") or "deterministic",
            ))
        analysis["content_opportunities"] = final_opportunities

        failed_source_count = sum(1 for x in source_discovery if x.get("status") == "failed")
        partial_source_count = sum(1 for x in source_discovery if x.get("status") == "partial")
        warning_source_count = sum(1 for x in source_discovery if x.get("status") == "warning")
        comparison_ready = bool((analysis.get("comparison_status") or {}).get("ready"))
        analysis["resolution_summary"] = live_resolution
        quality = result_quality(db, run_id, opportunity_count=len(final_opportunities))
        run.analysis_json = compact_analysis_payload(
            analysis,
            quality=quality,
            opportunity_count=len(final_opportunities),
        )

        unresolved = int(live_resolution.get("resolution_failed") or 0)
        stale = int(live_resolution.get("stale_404_410") or 0)
        has_warnings = bool(
            failed_source_count
            or partial_source_count
            or warning_source_count
            or discovery_failures
            or unresolved
            or stale
        )

        if not comparison_ready:
            run.status = "partial"
            run.phase = "partial"
            run.error = (
                "Partial sitemap analysis: comparison coverage is incomplete. "
                f"{(analysis.get('comparison_status') or {}).get('message', '')}"
            ).strip()
            final_message = "Sitemap Intelligence analysis finished with incomplete comparison coverage"
            final_phase = "partial"
        elif has_warnings:
            run.status = "completed_with_warnings"
            run.phase = "complete_with_warnings"
            run.error = None
            final_message = (
                "Sitemap Intelligence analysis complete with warnings: "
                f"{unresolved:,} unresolved URL(s), {stale:,} stale sitemap URL(s), "
                f"{failed_source_count + partial_source_count + warning_source_count} source warning state(s)"
            )
            final_phase = "complete_with_warnings"
        else:
            run.status = "completed"
            run.phase = "complete"
            run.error = None
            final_message = "Sitemap Intelligence analysis complete"
            final_phase = "complete"
        run.completed_at = _now()
        cfg = dict(run.config_json or {})

        # Recovery is a background concern. The main report is terminal and
        # exportable now; the worker will revisit only persisted recoverable rows.
        if settings.sitemap_background_recovery_enabled:
            from app.services.sitemap_recovery import _is_recoverable
            persisted_for_recovery = (
                db.query(models.SitemapUrl)
                .filter(models.SitemapUrl.sitemap_run_id == run_id)
                .all()
            )
            recoverable_count = sum(1 for row in persisted_for_recovery if _is_recoverable(row))
            cfg["_background_recovery"] = _background_recovery_metadata(
                run, recoverable_count
            )
        duplicate_record_count = int(
            db.query(models.SitemapDuplicate)
            .filter(models.SitemapDuplicate.sitemap_run_id == run_id)
            .count()
        )
        duplicate_audit = dict(cfg.get("_duplicate_audit") or {})
        duplicate_audit.update({
            "version": 1,
            "mode": duplicate_audit.get("mode") or "native",
            "completed_at": run.completed_at.isoformat(),
            "records": duplicate_record_count,
        })
        cfg["_duplicate_audit"] = duplicate_audit
        logger.info(
            "Sitemap canonical/duplicate audit persisted run_id=%s rows=%s",
            run_id,
            duplicate_record_count,
        )
        run.config_json = cfg
        db.commit()
        _progress(db, run, final_phase, 100, 100, 1, final_message)
    except SitemapRunCancellationRequested:
        db.rollback()
        try:
            run = db.get(models.SitemapRun, run_id)
            if run:
                run.status = "cancelled"
                run.phase = "cancelled"
                run.error = None
                run.completed_at = _now()
                cfg = dict(run.config_json or {})
                control = dict(cfg.get("_control") or {})
                control.update(
                    {
                        "cancel_requested": True,
                        "cancelled_at": _now().isoformat(),
                        "cancel_state": "cooperative",
                    }
                )
                cfg["_control"] = control
                run.config_json = cfg
                db.commit()
        except Exception:
            db.rollback()
        return

    except Exception as exc:
        # Any flush/commit error leaves a SQLAlchemy Session in failed transaction
        # state. Roll it back BEFORE touching ORM objects, otherwise error handling
        # itself raises PendingRollbackError and the run can remain falsely "running".
        db.rollback()
        try:
            run = db.get(models.SitemapRun, run_id)
            if run:
                run.status = "failed"
                run.phase = "failed"
                run.error = str(exc)
                run.completed_at = _now()
                db.commit()
        except Exception:
            db.rollback()
        raise
    finally:
        db.close()


