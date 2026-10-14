from __future__ import annotations

import json
import re
from io import BytesIO
from types import SimpleNamespace
from typing import Iterable

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from app import models
from app.services.sitemap_duplicate_audit import canonical_state
from app.services.sitemap_result_views import (
    cluster_audit_page,
    failure_page,
    head_to_head_page,
    missing_categories_page,
    topic_coverage_page,
    topic_taxonomy_page,
)

EXCEL_CELL_LIMIT = 32767
MAX_ROWS_PER_SHEET = 1_000_000


def _clean_text(value) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(value, (list, tuple, set)):
        value = " | ".join(str(x) for x in value)
    else:
        value = str(value)
    value = ILLEGAL_CHARACTERS_RE.sub("", value)
    # XML 1.0 also rejects several C0/C1 control characters that may not be
    # caught by older openpyxl versions.
    value = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", value)
    return value[:EXCEL_CELL_LIMIT]


def _append_rows_streaming(wb: Workbook, title: str, rows: Iterable[dict], headers: list[str] | None = None):
    sheet_index = 1
    ws = None
    row_count = 0

    def new_sheet():
        nonlocal ws, row_count, sheet_index
        suffix = "" if sheet_index == 1 else f" {sheet_index}"
        ws = wb.create_sheet((title[: 31 - len(suffix)] + suffix)[:31])
        row_count = 0
        sheet_index += 1

    new_sheet()
    iterator = iter(rows)
    first = next(iterator, None)
    if first is None:
        ws.append(["No data"])
        return
    headers = headers or list(first.keys())
    ws.append([_clean_text(x) for x in headers])
    row_count = 1

    def emit(row: dict):
        nonlocal ws, row_count
        if row_count >= MAX_ROWS_PER_SHEET:
            new_sheet()
            ws.append([_clean_text(x) for x in headers])
            row_count = 1
        ws.append([_clean_text(row.get(h)) for h in headers])
        row_count += 1

    emit(first)
    for row in iterator:
        emit(row)


def _analysis_rows(analysis: dict, key: str):
    for row in analysis.get(key) or []:
        yield row


def _inventory_rows(db, run_id: int, sources: dict[int, object], batch_size: int = 5000):
    last_id = 0
    while True:
        batch = (
            db.query(models.SitemapUrl)
            .filter(models.SitemapUrl.sitemap_run_id == run_id, models.SitemapUrl.id > last_id)
            .order_by(models.SitemapUrl.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break
        for x in batch:
            src = sources.get(x.source_id)
            evidence = x.classification_evidence_json or {}
            yield {
                "ID": x.id,
                "Site": src.label if src else x.source_id,
                "Role": src.role if src else "",
                "Root Sitemap": x.root_sitemap,
                "Child Sitemap": x.child_sitemap,
                "Original URL": x.original_url,
                "Normalized URL": x.normalized_url,
                "Final URL": x.final_url,
                "Canonical URL": x.canonical_url,
                "HTTP Status": x.http_status,
                "Broad Type": x.broad_page_type,
                "Detailed Type": x.detailed_page_type,
                "Type Confidence": x.type_confidence,
                "Classification Method": x.classification_method,
                "Classification Evidence": evidence,
                "HTML Truncated": evidence.get("html_truncated") or evidence.get("oversized_html") or False,
                "Category": x.category,
                "Topic": x.topic,
                "Subtopic": x.subtopic,
                "Topic Confidence": x.topic_confidence,
                "Topic Method": x.topic_method,
                "Title": x.title,
                "H1": x.h1_json,
                "H2": x.h2_json,
                "Schema Types": x.schema_types_json,
                "Breadcrumbs": x.breadcrumbs_json,
                "Word Count": x.word_count,
                "Author Present": x.author_present,
                "Published/Updated": x.published_at_text,
                "CTA Signals": x.cta_json,
                "Last Modified": x.lastmod,
                "Fetch Method": x.fetch_method,
                "Retries": x.retry_count,
                "Terminal State": evidence.get("terminal_state"),
                "Content Hash": x.content_hash,
                "Error": x.error,
            }
        last_id = batch[-1].id



def _canonical_audit_rows(db, run_id: int, sources: dict[int, object], batch_size: int = 5000):
    last_id = 0
    while True:
        batch = (
            db.query(models.SitemapUrl)
            .filter(models.SitemapUrl.sitemap_run_id == run_id, models.SitemapUrl.id > last_id)
            .order_by(models.SitemapUrl.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break
        for x in batch:
            src = sources.get(x.source_id)
            yield {
                "ID": x.id,
                "Site": src.label if src else x.source_id,
                "Role": src.role if src else "",
                "Root Sitemap": x.root_sitemap,
                "Child Sitemap": x.child_sitemap,
                "Original URL": x.original_url,
                "Normalized URL": x.normalized_url,
                "Final URL": x.final_url,
                "Canonical URL": x.canonical_url,
                "Canonical State": canonical_state(x),
                "HTTP Status": x.http_status,
                "Last Modified": x.lastmod,
                "Fetch Method": x.fetch_method,
                "Error": x.error,
            }
        last_id = batch[-1].id


def _duplicate_audit_rows(db, run_id: int, sources: dict[int, object], batch_size: int = 5000):
    last_id = 0
    while True:
        batch = (
            db.query(models.SitemapDuplicate)
            .filter(
                models.SitemapDuplicate.sitemap_run_id == run_id,
                models.SitemapDuplicate.id > last_id,
            )
            .order_by(models.SitemapDuplicate.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break
        for x in batch:
            primary = sources.get(x.primary_source_id)
            duplicate = sources.get(x.duplicate_source_id)
            yield {
                "ID": x.id,
                "Duplicate Kind": x.duplicate_kind,
                "Audit Stage": x.audit_stage,
                "Group Key": x.group_key,
                "Match URL": x.match_url,
                "Primary Site": primary.label if primary else x.primary_source_id,
                "Primary Role": primary.role if primary else "",
                "Duplicate Site": duplicate.label if duplicate else x.duplicate_source_id,
                "Duplicate Role": duplicate.role if duplicate else "",
                "Primary URL": x.primary_url,
                "Duplicate URL": x.duplicate_url,
                "Primary Child Sitemap": x.primary_child_sitemap,
                "Duplicate Child Sitemap": x.duplicate_child_sitemap,
                "Primary Final URL": x.primary_final_url,
                "Duplicate Final URL": x.duplicate_final_url,
                "Primary Canonical URL": x.primary_canonical_url,
                "Duplicate Canonical URL": x.duplicate_canonical_url,
                "Primary HTTP Status": x.primary_http_status,
                "Duplicate HTTP Status": x.duplicate_http_status,
                "Primary Last Modified": x.primary_lastmod,
                "Duplicate Last Modified": x.duplicate_lastmod,
            }
        last_id = batch[-1].id


def _opportunity_rows(db, run_id: int, batch_size: int = 5000):
    last_id = 0
    while True:
        batch = (
            db.query(models.SitemapOpportunity)
            .filter(models.SitemapOpportunity.sitemap_run_id == run_id, models.SitemapOpportunity.id > last_id)
            .order_by(models.SitemapOpportunity.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break
        for x in batch:
            yield {
                "Opportunity Key": x.opportunity_key,
                "Priority": x.priority,
                "Evidence Strength": x.confidence,
                "Gap Type": x.gap_type,
                "Category": x.category,
                "Topic": x.topic,
                "Recommended Page Type": x.recommended_page_type,
                "Client Count": x.client_count,
                "Competitor Count": x.competitor_count,
                "Competitor Sites": x.competitor_sites,
                "Business Relevance": x.business_relevance,
                "Title": x.title,
                "Recommendation": x.recommendation,
                "Evidence IDs": x.evidence_json,
                "Source": x.source,
            }
        last_id = batch[-1].id



def _sources_for_run(db, run) -> dict[int, object]:
    snapshot = dict((run.config_json or {}).get("_input_snapshot") or {})
    source_rows = list(snapshot.get("sources") or [])
    if source_rows:
        return {
            int(item["id"]): SimpleNamespace(**item)
            for item in source_rows
            if item.get("id") is not None
        }
    return {
        x.id: x
        for x in (
            db.query(models.SitemapSource)
            .filter_by(project_id=run.project_id)
            .order_by(models.SitemapSource.id)
            .all()
        )
    }


def _paged_view_rows(view_func, db, run_id: int, *, page_size: int = 500):
    """Exhaust one DB-backed paginated analytical view without JSON snapshots."""
    page = 1
    emitted = 0
    while True:
        result = view_func(db, run_id, page=page, page_size=page_size)
        items = list(result.get("items") or [])
        for row in items:
            yield row
        emitted += len(items)
        total = int(result.get("total") or 0)
        if not items or emitted >= total:
            break
        page += 1


def _discovery_issue_rows(run, analysis: dict):
    issues = (
        analysis.get("sitemap_failures")
        or (run.config_json or {}).get("_sitemap_discovery_issues")
        or []
    )
    for issue in issues:
        yield {
            "Site": issue.get("site"),
            "Role": issue.get("role"),
            "Stage": issue.get("stage") or "sitemap_discovery",
            "Root Sitemap": issue.get("root_sitemap"),
            "Parent Sitemap": issue.get("parent_sitemap"),
            "Child Sitemap": issue.get("url") or issue.get("sitemap"),
            "Final Sitemap": issue.get("final_sitemap"),
            "HTTP Status": issue.get("http_status"),
            "Fetch Method": issue.get("fetch_method"),
            "Retry Count": issue.get("retry_count", 0),
            "Verification Attempts": issue.get("verification_attempts", 0),
            "Empty Kind": issue.get("empty_kind"),
            "Severity": issue.get("severity"),
            "Code": issue.get("code"),
            "Error": issue.get("error"),
        }


def _failure_rows(db, run_id: int, *, page_size: int = 500):
    """Stream DB page failures plus compact sitemap-document audit warnings once."""
    page = 1
    emitted = 0
    supplemental_emitted = False
    while True:
        result = failure_page(db, run_id, page=page, page_size=page_size)
        if not supplemental_emitted:
            for issue in result.get("supplemental") or []:
                yield {
                    "Site": issue.get("site"),
                    "Role": issue.get("role"),
                    "Stage": issue.get("stage"),
                    "Root Sitemap": issue.get("root_sitemap"),
                    "Parent Sitemap": issue.get("parent_sitemap"),
                    "Child Sitemap": issue.get("child_sitemap"),
                    "URL": issue.get("url"),
                    "HTTP Status": issue.get("http_status"),
                    "Retry Count": issue.get("retry_count"),
                    "Verification Attempts": issue.get("verification_attempts"),
                    "Empty Kind": issue.get("empty_kind"),
                    "Terminal State": issue.get("terminal_state"),
                    "Severity": issue.get("severity"),
                    "Code": issue.get("code"),
                    "Fetch Method": issue.get("fetch_method"),
                    "Error": issue.get("error"),
                }
            supplemental_emitted = True

        items = list(result.get("items") or [])
        for issue in items:
            yield {
                "Site": issue.get("site"),
                "Role": issue.get("role"),
                "Stage": issue.get("stage"),
                "Root Sitemap": issue.get("root_sitemap"),
                "Parent Sitemap": issue.get("parent_sitemap"),
                "Child Sitemap": issue.get("child_sitemap"),
                "URL": issue.get("url"),
                "HTTP Status": issue.get("http_status"),
                "Retry Count": issue.get("retry_count"),
                "Verification Attempts": issue.get("verification_attempts"),
                "Empty Kind": issue.get("empty_kind"),
                "Terminal State": issue.get("terminal_state"),
                "Severity": issue.get("severity"),
                "Code": issue.get("code"),
                "Fetch Method": issue.get("fetch_method"),
                "Error": issue.get("error"),
            }
        emitted += len(items)
        total = int(result.get("total") or 0)
        if not items or emitted >= total:
            break
        page += 1


def _run_status_rows(db, run, analysis: dict):
    persisted_urls = (
        db.query(models.SitemapUrl)
        .filter(models.SitemapUrl.sitemap_run_id == run.id)
        .count()
    )
    pending_urls = (
        db.query(models.SitemapUrl)
        .filter(
            models.SitemapUrl.sitemap_run_id == run.id,
            models.SitemapUrl.error == "Pending page resolution",
        )
        .count()
    )
    failed_urls = (
        db.query(models.SitemapUrl)
        .filter(
            models.SitemapUrl.sitemap_run_id == run.id,
            models.SitemapUrl.error.is_not(None),
            models.SitemapUrl.error != "Pending page resolution",
        )
        .count()
    )
    opportunities = (
        db.query(models.SitemapOpportunity)
        .filter(models.SitemapOpportunity.sitemap_run_id == run.id)
        .count()
    )
    duplicates = (
        db.query(models.SitemapDuplicate)
        .filter(models.SitemapDuplicate.sitemap_run_id == run.id)
        .count()
    )
    discovery_issues = (
        analysis.get("sitemap_failures")
        or (run.config_json or {}).get("_sitemap_discovery_issues")
        or []
    )
    empty_children = sum(
        1
        for row in discovery_issues
        if str(row.get("code") or "") == "EMPTY_CHILD_SITEMAP"
    )
    bg = dict((run.config_json or {}).get("_background_recovery") or {})
    comparison = analysis.get("comparison_status") or {}

    if run.status == "completed":
        completeness = "COMPLETE"
    elif run.status == "completed_with_warnings":
        completeness = "COMPLETE_WITH_WARNINGS"
    elif run.status in {"queued", "claimed", "running", "cancelling"}:
        completeness = "IN_PROGRESS"
    else:
        completeness = "PARTIAL"

    yield {
        "Run ID": run.id,
        "Run Number": run.run_number,
        "Analysis State": completeness,
        "Run Status": run.status,
        "Phase": run.phase,
        "Created UTC": str(run.created_at or ""),
        "Started UTC": str(run.started_at or ""),
        "Completed UTC": str(run.completed_at or ""),
        "Persisted URLs": int(persisted_urls),
        "Processed/Persisted URLs": int(max(0, persisted_urls - pending_urls)),
        "Pending Resolution URLs": int(pending_urls),
        "Failed/Warning Page URLs": int(failed_urls),
        "Opportunities": int(opportunities),
        "Duplicate Audit Rows": int(duplicates),
        "Confirmed Empty Child Sitemaps": int(empty_children),
        "Comparison Ready": bool(comparison.get("ready")),
        "Comparison Message": comparison.get("message"),
        "Background Recovery State": bg.get("state"),
        "Background Recovery Next Attempt UTC": bg.get("next_attempt_at"),
        "Background Recovery Attempts": bg.get("slices_completed", 0),
        "Export Basis": "Persisted database snapshot + compact run metadata",
    }


def build_sitemap_workbook(db, run_id: int):
    """Export everything currently persisted for a sitemap run.

    Large analytical tabs are always rebuilt from the DB-backed result views.
    This intentionally works for completed, warning, partial, cancelled and
    in-progress runs.  `analysis_json` is used only for compact metadata and
    sitemap-document audit issues.
    """
    run = db.get(models.SitemapRun, run_id)
    if not run:
        raise ValueError(f"Sitemap run {run_id} not found")

    sources = _sources_for_run(db, run)
    analysis = run.analysis_json or {}

    wb = Workbook(write_only=True)

    _append_rows_streaming(wb, "Run Status", _run_status_rows(db, run, analysis))
    _append_rows_streaming(wb, "Sitemap Overview", _analysis_rows(analysis, "overview"))
    discovery_rows = (
        analysis.get("source_discovery")
        or (run.config_json or {}).get("_sitemap_discovery")
        or []
    )
    _append_rows_streaming(
        wb,
        "Discovery Scope",
        iter(discovery_rows),
    )
    _append_rows_streaming(wb, "Site Profiles", _analysis_rows(analysis, "site_profiles"))
    _append_rows_streaming(wb, "URL Type Summary", _analysis_rows(analysis, "url_type_summary"))

    _append_rows_streaming(
        wb,
        "Topic Taxonomy",
        _paged_view_rows(topic_taxonomy_page, db, run_id),
    )
    _append_rows_streaming(
        wb,
        "Dynamic Topic Clusters",
        _paged_view_rows(cluster_audit_page, db, run_id),
    )
    _append_rows_streaming(
        wb,
        "Topic Coverage Matrix",
        _paged_view_rows(topic_coverage_page, db, run_id),
    )
    _append_rows_streaming(
        wb,
        "Head-to-Head",
        _paged_view_rows(head_to_head_page, db, run_id),
    )
    _append_rows_streaming(
        wb,
        "Missing Categories",
        _paged_view_rows(missing_categories_page, db, run_id),
    )

    _append_rows_streaming(wb, "Sitemap Inventory", _inventory_rows(db, run_id, sources))
    _append_rows_streaming(wb, "Canonical Audit", _canonical_audit_rows(db, run_id, sources))
    _append_rows_streaming(wb, "Duplicate URL Audit", _duplicate_audit_rows(db, run_id, sources))
    _append_rows_streaming(wb, "Content Opportunities", _opportunity_rows(db, run_id))
    _append_rows_streaming(wb, "Sitemap Discovery Audit", _discovery_issue_rows(run, analysis))
    _append_rows_streaming(wb, "Sitemap Failures", _failure_rows(db, run_id))

    comparison = analysis.get("comparison_status") or {}
    _append_rows_streaming(wb, "Comparison Status", [comparison] if comparison else [])

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output
