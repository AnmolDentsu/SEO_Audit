from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import delete

from app import models
from app.services.sitemap_fetcher import normalize_url


SITEMAP_EXACT_DUPLICATE = "SITEMAP_EXACT_DUPLICATE"
SITEMAP_NORMALIZED_DUPLICATE = "SITEMAP_NORMALIZED_DUPLICATE"
CROSS_SOURCE_DUPLICATE = "CROSS_SOURCE_DUPLICATE"
CANONICAL_COLLISION = "CANONICAL_COLLISION"
REDIRECT_COLLISION = "REDIRECT_COLLISION"
RESOLVED_IDENTITY_COLLISION = "RESOLVED_IDENTITY_COLLISION"

SITEMAP_DUPLICATE_KINDS = {
    SITEMAP_EXACT_DUPLICATE,
    SITEMAP_NORMALIZED_DUPLICATE,
    CROSS_SOURCE_DUPLICATE,
}
RESOLUTION_COLLISION_KINDS = {
    CANONICAL_COLLISION,
    REDIRECT_COLLISION,
    RESOLVED_IDENTITY_COLLISION,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(value: str | None) -> str:
    return normalize_url(value or "") if value else ""


def _source_id(source) -> int:
    return int(getattr(source, "id", source))


def audit_group_key(run_id: int, duplicate_kind: str, match_url: str) -> str:
    raw = f"{int(run_id)}|{duplicate_kind}|{_norm(match_url) or match_url}".encode(
        "utf-8",
        "ignore",
    )
    return hashlib.sha256(raw).hexdigest()


def canonical_state(row: models.SitemapUrl) -> str:
    canonical = _norm(row.canonical_url)
    if not canonical:
        return "MISSING"

    final_or_original = _norm(row.final_url or row.original_url)
    if canonical == final_or_original:
        return "SELF_CANONICAL"

    from urllib.parse import urlparse

    final_host = (urlparse(row.final_url or row.original_url or "").hostname or "").lower()
    canonical_host = (urlparse(row.canonical_url or "").hostname or "").lower()
    if final_host and canonical_host and final_host != canonical_host:
        return "CROSS_HOST_CANONICAL"
    return "CANONICAL_TO_OTHER"


def resolved_collision_kind(primary_payload: dict, duplicate_payload: dict, match_url: str) -> str:
    match = _norm(match_url)

    primary_canonical = _norm(primary_payload.get("canonical_url"))
    duplicate_canonical = _norm(duplicate_payload.get("canonical_url"))
    if match and (
        (primary_canonical and primary_canonical == match)
        or (duplicate_canonical and duplicate_canonical == match)
    ):
        return CANONICAL_COLLISION

    primary_original = _norm(primary_payload.get("original_url"))
    duplicate_original = _norm(duplicate_payload.get("original_url"))
    primary_final = _norm(primary_payload.get("final_url") or primary_payload.get("original_url"))
    duplicate_final = _norm(duplicate_payload.get("final_url") or duplicate_payload.get("original_url"))
    if (
        match
        and primary_final == match
        and duplicate_final == match
        and (primary_final != primary_original or duplicate_final != duplicate_original)
    ):
        return REDIRECT_COLLISION

    return RESOLVED_IDENTITY_COLLISION


def _audit_row(
    *,
    run_id: int,
    primary_source_id: int,
    duplicate_source_id: int,
    duplicate_kind: str,
    audit_stage: str,
    match_url: str,
    primary_url: str,
    duplicate_url: str,
    primary_child_sitemap: str | None = None,
    duplicate_child_sitemap: str | None = None,
    primary_final_url: str | None = None,
    duplicate_final_url: str | None = None,
    primary_canonical_url: str | None = None,
    duplicate_canonical_url: str | None = None,
    primary_lastmod: str | None = None,
    duplicate_lastmod: str | None = None,
    primary_http_status: int | None = None,
    duplicate_http_status: int | None = None,
) -> models.SitemapDuplicate:
    return models.SitemapDuplicate(
        sitemap_run_id=int(run_id),
        primary_source_id=int(primary_source_id),
        duplicate_source_id=int(duplicate_source_id),
        duplicate_kind=duplicate_kind,
        audit_stage=audit_stage,
        group_key=audit_group_key(run_id, duplicate_kind, match_url),
        match_url=_norm(match_url) or str(match_url or ""),
        primary_url=str(primary_url or ""),
        duplicate_url=str(duplicate_url or ""),
        primary_child_sitemap=primary_child_sitemap,
        duplicate_child_sitemap=duplicate_child_sitemap,
        primary_final_url=primary_final_url,
        duplicate_final_url=duplicate_final_url,
        primary_canonical_url=primary_canonical_url,
        duplicate_canonical_url=duplicate_canonical_url,
        primary_lastmod=primary_lastmod,
        duplicate_lastmod=duplicate_lastmod,
        primary_http_status=primary_http_status,
        duplicate_http_status=duplicate_http_status,
        created_at=_now(),
    )


def add_discovery_duplicates(
    db,
    run_id: int,
    source,
    duplicates: Iterable[dict],
    *,
    audit_stage: str = "sitemap_discovery",
) -> int:
    source_id = _source_id(source)
    count = 0
    for item in duplicates or []:
        kind = str(item.get("duplicate_kind") or SITEMAP_NORMALIZED_DUPLICATE).upper()
        if kind not in {SITEMAP_EXACT_DUPLICATE, SITEMAP_NORMALIZED_DUPLICATE}:
            kind = SITEMAP_NORMALIZED_DUPLICATE
        db.add(
            _audit_row(
                run_id=run_id,
                primary_source_id=source_id,
                duplicate_source_id=source_id,
                duplicate_kind=kind,
                audit_stage=audit_stage,
                match_url=item.get("match_url") or item.get("duplicate_url") or "",
                primary_url=item.get("primary_url") or "",
                duplicate_url=item.get("duplicate_url") or "",
                primary_child_sitemap=item.get("primary_child_sitemap"),
                duplicate_child_sitemap=item.get("duplicate_child_sitemap"),
                primary_lastmod=item.get("primary_lastmod"),
                duplicate_lastmod=item.get("duplicate_lastmod"),
            )
        )
        count += 1
    return count


def add_cross_source_duplicate(
    db,
    run_id: int,
    primary_source,
    primary_entry,
    duplicate_source,
    duplicate_entry,
    *,
    audit_stage: str = "run_dedup",
) -> models.SitemapDuplicate:
    match_url = _norm(getattr(duplicate_entry, "url", "")) or getattr(duplicate_entry, "url", "")
    row = _audit_row(
        run_id=run_id,
        primary_source_id=_source_id(primary_source),
        duplicate_source_id=_source_id(duplicate_source),
        duplicate_kind=CROSS_SOURCE_DUPLICATE,
        audit_stage=audit_stage,
        match_url=match_url,
        primary_url=getattr(primary_entry, "url", "") or "",
        duplicate_url=getattr(duplicate_entry, "url", "") or "",
        primary_child_sitemap=getattr(primary_entry, "child_sitemap", None),
        duplicate_child_sitemap=getattr(duplicate_entry, "child_sitemap", None),
        primary_lastmod=getattr(primary_entry, "lastmod", None),
        duplicate_lastmod=getattr(duplicate_entry, "lastmod", None),
    )
    db.add(row)
    return row


def add_resolved_collision(
    db,
    run_id: int,
    primary_source,
    primary_payload: dict,
    duplicate_source,
    duplicate_payload: dict,
    match_url: str,
    *,
    audit_stage: str = "resolved_collision",
) -> models.SitemapDuplicate:
    kind = resolved_collision_kind(primary_payload, duplicate_payload, match_url)
    row = _audit_row(
        run_id=run_id,
        primary_source_id=_source_id(primary_source),
        duplicate_source_id=_source_id(duplicate_source),
        duplicate_kind=kind,
        audit_stage=audit_stage,
        match_url=match_url,
        primary_url=primary_payload.get("original_url") or "",
        duplicate_url=duplicate_payload.get("original_url") or "",
        primary_child_sitemap=primary_payload.get("child_sitemap"),
        duplicate_child_sitemap=duplicate_payload.get("child_sitemap"),
        primary_final_url=primary_payload.get("final_url"),
        duplicate_final_url=duplicate_payload.get("final_url"),
        primary_canonical_url=primary_payload.get("canonical_url"),
        duplicate_canonical_url=duplicate_payload.get("canonical_url"),
        primary_lastmod=primary_payload.get("lastmod"),
        duplicate_lastmod=duplicate_payload.get("lastmod"),
        primary_http_status=primary_payload.get("status_code"),
        duplicate_http_status=duplicate_payload.get("status_code"),
    )
    db.add(row)
    return row


def _row_payload(row: models.SitemapUrl) -> dict:
    return {
        "original_url": row.original_url,
        "final_url": row.final_url,
        "canonical_url": row.canonical_url,
        "child_sitemap": row.child_sitemap,
        "lastmod": row.lastmod,
        "status_code": row.http_status,
    }


def rebuild_persisted_collision_audit(
    db,
    run_id: int,
    rows: Iterable[models.SitemapUrl],
    *,
    audit_stage: str = "recovery_refresh",
) -> int:
    """Rebuild collision evidence that can be derived from currently persisted rows.

    Native run-time collision rows are never deleted here. Those rows may contain
    losing candidate URLs that were intentionally not inserted into SitemapUrl.
    This refresh stage only captures *new* collisions that become visible after a
    failed row is recovered and receives a final/canonical URL.
    """
    db.execute(
        delete(models.SitemapDuplicate).where(
            models.SitemapDuplicate.sitemap_run_id == int(run_id),
            models.SitemapDuplicate.audit_stage == audit_stage,
        )
    )

    first_by_identity: dict[str, models.SitemapUrl] = {}
    created = 0
    for row in rows:
        identity = _norm(row.canonical_url or row.final_url or row.original_url)
        if not identity:
            continue

        first = first_by_identity.get(identity)
        if first is None:
            first_by_identity[identity] = row
            continue

        if _norm(first.original_url) == _norm(row.original_url):
            continue

        add_resolved_collision(
            db,
            run_id,
            first.source_id,
            _row_payload(first),
            row.source_id,
            _row_payload(row),
            identity,
            audit_stage=audit_stage,
        )
        created += 1

    return created
