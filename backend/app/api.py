import asyncio
import csv
import io
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import get_settings
from app.database import get_db
from app.services.benchmarks import benchmark_configuration, build_density_benchmarks, density_target_status
from app.services.content_analysis import build_detailed_analysis
from app.services.cloud_run import dispatch_cloud_run_job
from app.services.export import build_run_workbook
from app.services.google_auth import auth_summary, probe_vertex_or_api_key
from app.services.job_runner import execute_run
from app.services.sitemap_result_views import (
    canonical_audit_page,
    cluster_audit_page,
    duplicate_audit_page,
    failure_page,
    head_to_head_page,
    missing_categories_page,
    opportunities_page,
    topic_coverage_page,
    topic_taxonomy_page,
)

from app.services.compliance_guard import (
    validate_non_sensitive_text,
    validate_public_url_shape,
)

from app.services.queue_eta import (
    enforce_project_backlog,
)

from app.services.security import (
    validate_public_url,
)


router = APIRouter(prefix="/api")
settings = get_settings()

MAX_KEYWORD_CSV_BYTES = 5 * 1024 * 1024
MAX_KEYWORD_CSV_ROWS = 10_000


def project_or_404(db, project_id):
    obj = db.get(models.Project, project_id)
    if not obj:
        raise HTTPException(404, "Project not found")
    return obj


def run_or_404(db, run_id):
    obj = db.get(models.Run, run_id)
    if not obj:
        raise HTTPException(404, "Run not found")
    return obj



_LIVE_RUN_STATUSES = {"queued", "claimed", "running", "cancelling"}
_TERMINAL_PAGE_STATUSES = {"completed", "failed", "dispatch_failed", "cancelled"}
_TERMINAL_SITEMAP_STATUSES = {
    "completed",
    "completed_with_warnings",
    "partial",
    "failed",
    "dispatch_failed",
    "cancelled",
}


def _utcnow():
    return datetime.now(timezone.utc)


def _project_dict(row: models.Project) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "client_name": row.client_name,
        "parent_project_id": row.parent_project_id,
        "created_at": row.created_at,
    }


def _validate_parent_project(db: Session, project_id: int | None, parent_project_id: int | None) -> None:
    if parent_project_id is None:
        return
    if project_id is not None and parent_project_id == project_id:
        raise HTTPException(400, "A project cannot be its own parent")

    parent = db.get(models.Project, parent_project_id)
    if not parent:
        raise HTTPException(404, "Parent project not found")

    # Prevent cycles when moving an existing project inside the hierarchy.
    if project_id is not None:
        seen = {project_id}
        cursor = parent
        while cursor is not None:
            if cursor.id in seen:
                raise HTTPException(400, "Project hierarchy would create a cycle")
            seen.add(cursor.id)
            if cursor.parent_project_id is None:
                break
            cursor = db.get(models.Project, cursor.parent_project_id)


def _next_run_number(db: Session, model, project_id: int) -> int:
    # Lock the parent project row on PostgreSQL so two API instances cannot
    # allocate the same project-local run number concurrently. SQLite already
    # serialises writers, so this remains safe in local development.
    (
        db.query(models.Project)
        .filter(models.Project.id == project_id)
        .with_for_update()
        .first()
    )
    current = (
        db.query(func.max(model.run_number))
        .filter(model.project_id == project_id)
        .scalar()
    )
    return int(current or 0) + 1


def _page_input_snapshot(targets, keywords) -> dict:
    return {
        "targets": [
            {
                "id": int(x.id),
                "url": str(x.url),
                "role": str(x.role or "OTHER"),
                "label": x.label,
            }
            for x in targets
        ],
        "keywords": [
            {
                "id": int(x.id),
                "keyword": str(x.keyword),
                "keyword_type": str(x.keyword_type or "OTHER"),
                "search_volume": x.search_volume,
                "priority": str(x.priority or "NONE"),
                "target_min": x.target_min,
                "target_max": x.target_max,
            }
            for x in keywords
        ],
    }


def _sitemap_input_snapshot(sources) -> dict:
    return {
        "sources": [
            {
                "id": int(x.id),
                "root_url": str(x.root_url),
                "role": str(x.role or "COMPETITOR"),
                "label": str(x.label or ""),
            }
            for x in sources
        ],
    }


def _page_run_inputs(db: Session, run: models.Run):
    snapshot = dict((run.config_json or {}).get("_input_snapshot") or {})
    target_rows = list(snapshot.get("targets") or [])
    keyword_rows = list(snapshot.get("keywords") or [])

    if target_rows:
        targets = {
            int(x["id"]): SimpleNamespace(**x)
            for x in target_rows
            if x.get("id") is not None
        }
    else:
        targets = {
            x.id: x
            for x in db.query(models.Target).filter_by(project_id=run.project_id).all()
        }

    if keyword_rows:
        keywords = {
            int(x["id"]): SimpleNamespace(**x)
            for x in keyword_rows
            if x.get("id") is not None
        }
    else:
        keywords = {
            x.id: x
            for x in db.query(models.Keyword).filter_by(project_id=run.project_id).all()
        }

    return targets, keywords


def _sitemap_run_sources(db: Session, run: models.SitemapRun):
    snapshot = dict((run.config_json or {}).get("_input_snapshot") or {})
    source_rows = list(snapshot.get("sources") or [])
    if source_rows:
        return {
            int(x["id"]): SimpleNamespace(**x)
            for x in source_rows
            if x.get("id") is not None
        }
    return {
        x.id: x
        for x in db.query(models.SitemapSource).filter_by(project_id=run.project_id).all()
    }


def _request_cancel(row) -> None:
    if row.status in {
        "completed",
        "completed_with_warnings",
        "partial",
        "failed",
        "dispatch_failed",
        "cancelled",
    }:
        return

    cfg = dict(row.config_json or {})
    control = dict(cfg.get("_control") or {})
    control.update(
        {
            "cancel_requested": True,
            "cancel_requested_at": _utcnow().isoformat(),
            "previous_status": row.status,
            "previous_phase": row.phase,
        }
    )
    cfg["_control"] = control
    row.config_json = cfg
    row.error = None

    if row.status == "queued":
        row.status = "cancelled"
        row.phase = "cancelled"
        row.completed_at = _utcnow()
    else:
        row.status = "cancelling"
        # The worker supervisor will terminate the complete child process group,
        # including Playwright/Chromium descendants, then persist "cancelled".
        row.phase = "cancelling"


def _reset_page_configuration(db: Session, project_id: int) -> dict:
    live_runs = (
        db.query(models.Run)
        .filter(
            models.Run.project_id == project_id,
            models.Run.status.in_(_LIVE_RUN_STATUSES),
        )
        .all()
    )
    for run in live_runs:
        _request_cancel(run)

    targets = (
        db.query(models.Target)
        .filter_by(project_id=project_id, enabled=True)
        .all()
    )
    keywords = (
        db.query(models.Keyword)
        .filter_by(project_id=project_id, enabled=True)
        .all()
    )

    for row in targets:
        row.enabled = False
    for row in keywords:
        row.enabled = False

    return {
        "cancel_requested_runs": [x.id for x in live_runs],
        "targets_disabled": len(targets),
        "keywords_disabled": len(keywords),
    }


def _reset_sitemap_configuration(db: Session, project_id: int) -> dict:
    live_runs = (
        db.query(models.SitemapRun)
        .filter(
            models.SitemapRun.project_id == project_id,
            models.SitemapRun.status.in_(_LIVE_RUN_STATUSES),
        )
        .all()
    )
    for run in live_runs:
        _request_cancel(run)

    sources = (
        db.query(models.SitemapSource)
        .filter_by(project_id=project_id, enabled=True)
        .all()
    )
    for row in sources:
        row.enabled = False

    return {
        "cancel_requested_runs": [x.id for x in live_runs],
        "sitemap_sources_disabled": len(sources),
    }


def _confidence_band(value: float | None) -> str:
    score = float(value or 0)
    if score >= 0.90:
        return "STRONG"
    if score >= 0.80:
        return "HIGH"
    if score >= 0.70:
        return "MODERATE"
    return "REVIEW"


def keyword_dict(row: models.Keyword) -> dict:
    return {
        "id": row.id,
        "keyword": row.keyword,
        "keyword_type": (row.keyword_type or "OTHER").upper(),
        "search_volume": row.search_volume,
    }


def _normalize_header(value: str) -> str:
    value = (value or "").replace("\ufeff", "").strip().casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


HEADER_ALIASES = {
    "keyword": {"keyword", "keywords", "keyword phrase", "key phrase", "phrase"},
    "keyword_type": {"keyword type", "keywordtype", "type", "kw type"},
    "search_volume": {"search volume", "searchvolume", "volume", "monthly search volume", "sv"},
}


def _find_header(fieldnames: list[str], canonical: str) -> str | None:
    alias_set = HEADER_ALIASES[canonical]
    for raw in fieldnames:
        if _normalize_header(raw) in alias_set:
            return raw
    return None


def _parse_search_volume(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[\s,]", "", text)
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Search Volume must be numeric; received {text!r}") from exc
    if number < 0:
        raise ValueError("Search Volume cannot be negative")
    return int(round(number))


def _normalize_keyword_type(value) -> str:
    text = str(value or "OTHER").strip().upper()
    aliases = {
        "": "OTHER",
        "NONE": "OTHER",
        "PRIMARY KEYWORD": "PRIMARY",
        "SECONDARY KEYWORD": "SECONDARY",
    }
    text = aliases.get(text, text)
    if text not in schemas.VALID_KEYWORD_TYPES:
        raise ValueError(
            f"Keyword Type must be Primary, Secondary or Other; received {value!r}"
        )
    return text


def _public_ai_source(value: str | None) -> str:
    """Map internal/vendor-specific historical source labels to client-safe labels."""
    text = str(value or "")
    mapping = {
        "gemini": "ai-model",
        "gemini-new": "ai-model-new",
        "deterministic+gemini-reviewed": "deterministic+ai-reviewed",
    }
    return mapping.get(text, text.replace("gemini", "ai-model"))

def _public_ai_metadata(payload: dict | None) -> dict:
    """Remove provider/model implementation details from client-facing responses."""
    data = dict(payload or {})
    hidden_keys = {
        "model", "model_name", "gemini_model", "model_text",
        "embedding_model", "model_embedding", "provider_model",
    }
    for key in list(data):
        if key.lower() in hidden_keys:
            data.pop(key, None)
    data["analysis_engine"] = "AI model"
    return data

async def _require_public_web_url(
    value: str,
    *,
    field_name: str,
) -> None:
    try:
        validate_public_url_shape(
            value
        )

        await validate_public_url(
            value
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field_name} is not an allowed "
                f"public-web URL: {exc}"
            ),
        ) from exc


@router.get("/ai/status")
def ai_status():
    """Return client-safe AI/auth status without exposing model identifiers."""
    return _public_ai_metadata(auth_summary().safe_dict())


@router.post("/ai/test")
def ai_test():
    """Perform one minimal AI-model request to validate configured authentication."""
    try:
        result = probe_vertex_or_api_key()
        return _public_ai_metadata(result if isinstance(result, dict) else {"ok": True})
    except Exception as exc:
        raise HTTPException(502, f"AI authentication/model test failed: {exc}")


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    rows = db.query(models.Project).order_by(models.Project.created_at.desc()).all()
    return [_project_dict(p) for p in rows]


@router.post("/projects", status_code=201)
def create_project(body: schemas.ProjectCreate, db: Session = Depends(get_db)):
    _validate_parent_project(db, None, body.parent_project_id)
    row = models.Project(
        name=body.name.strip(),
        client_name=(body.client_name or "").strip() or None,
        parent_project_id=body.parent_project_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _project_dict(row)


@router.patch("/projects/{project_id}")
def update_project(
    project_id: int,
    body: schemas.ProjectUpdate,
    db: Session = Depends(get_db),
):
    row = project_or_404(db, project_id)
    supplied = body.model_fields_set

    if "parent_project_id" in supplied:
        _validate_parent_project(db, project_id, body.parent_project_id)
        row.parent_project_id = body.parent_project_id

    if "name" in supplied and body.name is not None:
        row.name = body.name.strip()

    if "client_name" in supplied:
        row.client_name = (body.client_name or "").strip() or None

    db.commit()
    db.refresh(row)
    return _project_dict(row)


@router.post("/projects/{project_id}/subprojects", status_code=201)
def create_subproject(
    project_id: int,
    body: schemas.ProjectCreate,
    db: Session = Depends(get_db),
):
    project_or_404(db, project_id)
    row = models.Project(
        name=body.name.strip(),
        client_name=(body.client_name or "").strip() or None,
        parent_project_id=project_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _project_dict(row)


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    p = project_or_404(db, project_id)

    targets = (
        db.query(models.Target)
        .filter_by(project_id=p.id, enabled=True)
        .order_by(models.Target.id)
        .all()
    )
    keywords = (
        db.query(models.Keyword)
        .filter_by(project_id=p.id, enabled=True)
        .order_by(models.Keyword.id)
        .all()
    )
    runs = (
        db.query(models.Run)
        .filter_by(project_id=p.id)
        .order_by(models.Run.run_number.desc(), models.Run.id.desc())
        .limit(20)
        .all()
    )
    subprojects = (
        db.query(models.Project)
        .filter_by(parent_project_id=p.id)
        .order_by(models.Project.created_at, models.Project.id)
        .all()
    )

    return {
        **_project_dict(p),
        "subprojects": [_project_dict(x) for x in subprojects],
        "targets": [
            {
                "id": x.id,
                "url": x.url,
                "role": x.role,
                "label": x.label,
            }
            for x in targets
        ],
        "keywords": [keyword_dict(x) for x in keywords],
        "benchmark_configuration": benchmark_configuration(targets),
        "runs": [run_dict(r) for r in runs],
    }


@router.post("/projects/{project_id}/page-analysis/reset")
def reset_page_analysis(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    result = _reset_page_configuration(db, project_id)
    db.commit()
    return {
        "status": "reset",
        "project_id": project_id,
        **result,
        "message": (
            "Page Analysis inputs were reset. Historical runs were preserved. "
            "Any live run received a cancellation request."
        ),
    }


@router.post("/projects/{project_id}/sitemap-analysis/reset")
def reset_sitemap_analysis(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    result = _reset_sitemap_configuration(db, project_id)
    db.commit()
    return {
        "status": "reset",
        "project_id": project_id,
        **result,
        "message": (
            "Sitemap inputs were reset. Historical runs were preserved. "
            "Any live run received a cancellation request."
        ),
    }


@router.post(
    "/projects/{project_id}/targets",
    status_code=201,
)
async def add_target(
    project_id: int,
    body: schemas.TargetCreate,
    db: Session = Depends(get_db),
):
    project_or_404(
        db,
        project_id,
    )

    url = str(
        body.url
    )

    await _require_public_web_url(
        url,
        field_name="Page URL",
    )

    existing = (
        db.query(models.Target)
        .filter_by(
            project_id=project_id,
            url=url,
        )
        .first()
    )

    if existing:
        if existing.enabled:
            raise HTTPException(
                409,
                "URL already exists in this project",
            )

        existing.enabled = True
        existing.role = body.role
        existing.label = body.label
        row = existing

    else:
        row = models.Target(
            project_id=project_id,
            url=url,
            role=body.role,
            label=body.label,
            enabled=True,
        )

        db.add(row)

    db.commit()
    db.refresh(row)

    return {
        "id": row.id,
        "url": row.url,
        "role": row.role,
        "label": row.label,
    }


@router.delete("/targets/{target_id}", status_code=204)
def delete_target(target_id: int, db: Session = Depends(get_db)):
    row = db.get(models.Target, target_id)
    if not row:
        raise HTTPException(404, "Target not found")

    # Soft-disable so historical PageResult rows remain auditable.
    row.enabled = False
    db.commit()


@router.post("/projects/{project_id}/keywords", status_code=201)
def add_keyword(project_id: int, body: schemas.KeywordCreate, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    keyword = body.keyword.strip()

    existing = (
        db.query(models.Keyword)
        .filter_by(project_id=project_id, keyword=keyword)
        .first()
    )

    if existing:
        if existing.enabled:
            raise HTTPException(409, "Keyword already exists in this project")
        existing.enabled = True
        existing.keyword_type = body.keyword_type
        existing.search_volume = body.search_volume
        row = existing
    else:
        row = models.Keyword(
            project_id=project_id,
            keyword=keyword,
            keyword_type=body.keyword_type,
            search_volume=body.search_volume,
            priority="NONE",
            target_min=None,
            target_max=None,
            enabled=True,
        )
        db.add(row)

    db.commit()
    db.refresh(row)
    return keyword_dict(row)


@router.post("/projects/{project_id}/keywords/import-csv")
async def import_keywords_csv(
    project_id: int,
    file: UploadFile = File(...),
    mode: str = "merge",
    db: Session = Depends(get_db),
):
    """
    Import project keywords from CSV.

    mode=merge
        Insert new keywords and update supplied metadata for existing keywords.
        Keywords absent from the CSV remain active.

    mode=replace
        The uploaded CSV becomes the active keyword set for this project.
        Existing rows are soft-disabled rather than deleted, preserving all
        historical run evidence and foreign-key references.

    mode=add_only
        Add/reactivate keywords that are not currently active. Existing active
        keyword metadata is left unchanged.

    Supported headers:
        Keyword / Keywords
        Keyword Type / keyword_type / Type
        Search Volume / search_volume / Volume / SV

    Blank Keyword Type/Search Volume values preserve existing metadata.
    """
    project_or_404(db, project_id)

    mode = str(mode or "merge").strip().lower()
    aliases = {
        "add": "add_only",
        "add-only": "add_only",
        "add_only": "add_only",
        "merge": "merge",
        "replace": "replace",
    }
    mode = aliases.get(mode, mode)
    if mode not in {"merge", "replace", "add_only"}:
        raise HTTPException(
            422,
            "CSV import mode must be one of: merge, replace, add_only",
        )

    raw = await file.read(MAX_KEYWORD_CSV_BYTES + 1)
    if len(raw) > MAX_KEYWORD_CSV_BYTES:
        raise HTTPException(413, "CSV is too large; maximum size is 5 MB")
    if not raw.strip():
        raise HTTPException(400, "CSV file is empty")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                400,
                "CSV must be UTF-8 or Windows-1252 text",
            ) from exc

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fieldnames = reader.fieldnames or []

    keyword_col = _find_header(fieldnames, "keyword")
    type_col = _find_header(fieldnames, "keyword_type")
    volume_col = _find_header(fieldnames, "search_volume")

    if not keyword_col:
        raise HTTPException(
            422,
            {
                "message": "CSV is missing a keyword column.",
                "accepted_keyword_headers": sorted(HEADER_ALIASES["keyword"]),
                "received_headers": [_normalize_header(h) for h in fieldnames],
            },
        )

    parsed_rows: list[dict] = []
    errors: list[dict] = []
    seen_in_file: set[str] = set()
    skipped_blank = 0
    skipped_file_duplicates = 0

    for index, row in enumerate(reader, start=2):
        if index - 1 > MAX_KEYWORD_CSV_ROWS:
            raise HTTPException(
                413,
                f"CSV exceeds maximum {MAX_KEYWORD_CSV_ROWS} data rows",
            )

        keyword = " ".join(str(row.get(keyword_col) or "").split())
        if not keyword:
            skipped_blank += 1
            continue

        try:
            keyword = validate_non_sensitive_text(
                keyword,
                field_name=f"CSV keyword at row {index}",
            ) or ""
        except ValueError as exc:
            errors.append(
                {
                    "row": index,
                    "keyword": keyword[:120],
                    "error": str(exc),
                }
            )
            continue

        key = keyword.casefold()
        if key in seen_in_file:
            skipped_file_duplicates += 1
            continue
        seen_in_file.add(key)

        raw_type = row.get(type_col) if type_col else None
        raw_volume = row.get(volume_col) if volume_col else None
        raw_type_text = str(raw_type).strip() if raw_type is not None else ""
        raw_volume_text = str(raw_volume).strip() if raw_volume is not None else ""

        try:
            parsed_type = (
                _normalize_keyword_type(raw_type_text)
                if raw_type_text
                else None
            )
            parsed_volume = (
                _parse_search_volume(raw_volume_text)
                if raw_volume_text
                else None
            )
        except ValueError as exc:
            errors.append(
                {
                    "row": index,
                    "keyword": keyword,
                    "error": str(exc),
                }
            )
            continue

        parsed_rows.append(
            {
                "keyword": keyword,
                "key": key,
                "keyword_type": parsed_type,
                "search_volume": parsed_volume,
                "type_supplied": bool(raw_type_text),
                "volume_supplied": bool(raw_volume_text),
            }
        )

    if errors and not parsed_rows:
        raise HTTPException(
            422,
            {
                "message": "No valid keyword rows could be imported.",
                "errors": errors[:50],
            },
        )

    existing_rows = (
        db.query(models.Keyword)
        .filter_by(project_id=project_id)
        .all()
    )
    existing_by_keyword = {
        str(row.keyword).strip().casefold(): row
        for row in existing_rows
    }

    active_before = {row.id for row in existing_rows if row.enabled}

    # REPLACE means "this CSV is the active keyword set". We soft-disable first,
    # then reactivate/upsert every valid row from the file.
    if mode == "replace":
        for row in existing_rows:
            row.enabled = False

    imported = 0
    updated = 0
    unchanged = 0
    reactivated = 0
    search_volume_updated = 0
    keyword_type_updated = 0

    for item in parsed_rows:
        existing = existing_by_keyword.get(item["key"])

        if existing is None:
            new_row = models.Keyword(
                project_id=project_id,
                keyword=item["keyword"],
                keyword_type=item["keyword_type"] or "OTHER",
                search_volume=item["search_volume"],
                priority="NONE",
                target_min=None,
                target_max=None,
                enabled=True,
            )
            db.add(new_row)
            existing_by_keyword[item["key"]] = new_row
            imported += 1
            continue

        was_enabled = bool(existing.enabled)
        changed = False

        if not existing.enabled:
            existing.enabled = True
            reactivated += 1
            changed = True

        if mode != "add_only":
            if (
                item["type_supplied"]
                and existing.keyword_type != item["keyword_type"]
            ):
                existing.keyword_type = item["keyword_type"]
                keyword_type_updated += 1
                changed = True

            if (
                item["volume_supplied"]
                and existing.search_volume != item["search_volume"]
            ):
                existing.search_volume = item["search_volume"]
                search_volume_updated += 1
                changed = True

        if changed:
            updated += 1
        elif was_enabled:
            unchanged += 1

    db.flush()

    active_after_rows = (
        db.query(models.Keyword)
        .filter_by(project_id=project_id, enabled=True)
        .all()
    )
    active_after = {row.id for row in active_after_rows}
    deactivated = len(active_before - active_after)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            409,
            "Keyword import could not be committed because of a database uniqueness conflict",
        ) from exc

    return {
        "filename": file.filename,
        "mode": mode,
        "imported": imported,
        "updated": updated,
        "unchanged": unchanged,
        "reactivated": reactivated,
        "deactivated": deactivated,
        "skipped_duplicates": skipped_file_duplicates,
        "skipped_file_duplicates": skipped_file_duplicates,
        "skipped_blank": skipped_blank,
        "search_volume_updated": search_volume_updated,
        "keyword_type_updated": keyword_type_updated,
        "error_count": len(errors),
        "errors": errors[:50],
        "active_keyword_count": len(active_after),
        "detected_columns": {
            "keyword": keyword_col,
            "keyword_type": type_col,
            "search_volume": volume_col,
        },
    }


@router.delete("/keywords/{keyword_id}", status_code=204)
def delete_keyword(keyword_id: int, db: Session = Depends(get_db)):
    row = db.get(models.Keyword, keyword_id)
    if not row:
        raise HTTPException(404, "Keyword not found")

    # Soft-disable so historical DensityResult rows remain intact.
    row.enabled = False
    db.commit()


def run_dict(r):
    return {
        "id": r.id,
        "run_number": r.run_number,
        "project_id": r.project_id,
        "status": r.status,
        "phase": r.phase,
        "progress_current": r.progress_current,
        "progress_total": r.progress_total,
        "error": r.error,
        "created_at": r.created_at,
        "started_at": r.started_at,
        "completed_at": r.completed_at,
        "config": r.config_json,
    }


@router.post("/projects/{project_id}/runs", status_code=201)
async def create_run(
    project_id: int,
    body: schemas.RunCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    project_or_404(db, project_id)

    targets = (
        db.query(models.Target)
        .filter_by(project_id=project_id, enabled=True)
        .order_by(models.Target.id)
        .all()
    )
    keywords = (
        db.query(models.Keyword)
        .filter_by(project_id=project_id, enabled=True)
        .order_by(models.Keyword.id)
        .all()
    )

    clients = [t for t in targets if t.role == "CLIENT"]
    p0s = [t for t in targets if t.role == "P0"]
    p1s = [t for t in targets if t.role == "P1"]

    if len(clients) != 1:
        raise HTTPException(400, "Configure exactly one CLIENT URL")
    if len(p0s) > 1:
        raise HTTPException(400, "Use at most one P0 benchmark anchor")
    if len(p1s) > 1:
        raise HTTPException(400, "Use at most one P1 benchmark anchor")
    if len(targets) > settings.run_max_urls:
        raise HTTPException(
            400,
            f"Project exceeds RUN_MAX_URLS={settings.run_max_urls}",
        )
    if not keywords:
        raise HTTPException(400, "Add at least one keyword")

    # Re-check every persisted target at run-request time. This prevents
    # legacy/stale records from bypassing the current public-web boundary.
    await asyncio.gather(
        *[
            _require_public_web_url(
                str(target.url),
                field_name=f"{target.role} URL",
            )
            for target in targets
        ]
    )

    try:
        enforce_project_backlog(
            db,
            "page",
            project_id,
        )
    except ValueError as exc:
        raise HTTPException(
            429,
            str(exc),
        ) from exc

    config = body.model_dump()
    config["_input_snapshot"] = _page_input_snapshot(targets, keywords)

    row = models.Run(
        project_id=project_id,
        run_number=_next_run_number(db, models.Run, project_id),
        status="queued",
        progress_total=len(targets),
        config_json=config,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    if settings.job_execution_mode == "inline":
        background_tasks.add_task(asyncio.run, execute_run(row.id))
    elif settings.job_execution_mode == "cloud_run_job":
        try:
            dispatch_cloud_run_job(row.id)
        except Exception as exc:
            row.status = "dispatch_failed"
            row.error = f"Cloud Run Job dispatch failed: {exc}"
            db.commit()
            raise HTTPException(502, row.error)

    return run_dict(row)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, db: Session = Depends(get_db)):
    row = run_or_404(db, run_id)
    _request_cancel(row)
    db.commit()
    db.refresh(row)
    return run_dict(row)


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    return run_dict(run_or_404(db, run_id))


@router.get("/runs/{run_id}/phase1")
def phase1(run_id: int, db: Session = Depends(get_db)):
    run = run_or_404(db, run_id)
    targets, keywords = _page_run_inputs(db, run)
    page_rows = db.query(models.PageResult).filter_by(run_id=run_id).all()
    pages = {p.id: p for p in page_rows}
    rows = (
        db.query(models.DensityResult)
        .join(models.PageResult)
        .filter(models.PageResult.run_id == run_id)
        .all()
    )
    benchmarks = build_density_benchmarks(page_rows, rows, targets)

    output = []
    for d in rows:
        page = pages[d.page_result_id]
        target = targets[page.target_id]
        kw = keywords[d.keyword_id]
        benchmark = benchmarks.get(kw.id, {})
        output.append(
            {
                "evidence_id": f"density:{d.id}",
                "role": target.role,
                "label": target.label,
                "url": page.url,
                "keyword": kw.keyword,
                "keyword_type": (kw.keyword_type or "OTHER").upper(),
                "search_volume": kw.search_volume,
                "count": d.count,
                "word_count": page.word_count,
                "density_pct": d.density_pct,
                "target_min": benchmark.get("target_min"),
                "target_max": benchmark.get("target_max"),
                "target_min_source": benchmark.get("target_min_source"),
                "target_max_source": benchmark.get("target_max_source"),
                "benchmark_state": benchmark.get("benchmark_state", "NO_ANCHORS"),
                "benchmark_ready": bool(benchmark.get("benchmark_ready")),
                "other_benchmark_count": benchmark.get("other_count", 0),
                "other_benchmark_median": benchmark.get("other_median"),
                "other_benchmark_q1": benchmark.get("other_q1"),
                "other_benchmark_q3": benchmark.get("other_q3"),
                "competitor_pool_count": benchmark.get("competitor_count", 0),
                "competitor_pool_median": benchmark.get("competitor_median"),
                "competitor_pool_q1": benchmark.get("competitor_q1"),
                "competitor_pool_q3": benchmark.get("competitor_q3"),
                "target_status": density_target_status(target.role, d.density_pct, benchmark),
                "present": d.present,
            }
        )
    return output


@router.get("/runs/{run_id}/phase2")
def phase2(run_id: int, db: Session = Depends(get_db)):
    run = run_or_404(db, run_id)
    targets, _ = _page_run_inputs(db, run)
    pages = db.query(models.PageResult).filter_by(run_id=run_id).all()
    return [
        {
            "evidence_id": f"page:{p.id}",
            "role": targets[p.target_id].role,
            "label": targets[p.target_id].label,
            "url": p.url,
            "final_url": p.final_url,
            "status_code": p.status_code,
            "word_count": p.word_count,
            "title": p.title,
            "meta_description": p.meta_description,
            "h1": p.h1_json,
            "h2": p.h2_json,
            "h3": p.h3_json,
            "retry_count": p.retry_count,
            "from_cache": p.from_cache,
            "error": p.error,
            "fetched_at": p.fetched_at,
        }
        for p in pages
    ]


@router.get("/runs/{run_id}/detailed-analysis")
def detailed_analysis(run_id: int, db: Session = Depends(get_db)):
    run = run_or_404(db, run_id)
    targets, keyword_map = _page_run_inputs(db, run)
    pages = db.query(models.PageResult).filter_by(run_id=run_id).order_by(models.PageResult.id).all()
    keywords = list(keyword_map.values())
    opportunities_rows = db.query(models.Opportunity).filter_by(run_id=run_id).order_by(models.Opportunity.id).all()
    analysis = run.analysis_json or {}
    required = {"content_structure", "section_comparison", "keyword_placement", "faq_comparison", "content_opportunities"}
    if not required.issubset(set(analysis.keys())):
        analysis = build_detailed_analysis(pages, targets, keywords, opportunities_rows)
    return analysis


@router.get("/runs/{run_id}/opportunities")
def opportunities(run_id: int, db: Session = Depends(get_db)):
    run_or_404(db, run_id)
    rows = db.query(models.Opportunity).filter_by(run_id=run_id).order_by(models.Opportunity.id).all()
    return [
        {
            "id": o.id,
            "category": o.category,
            "priority": o.priority,
            "confidence": o.confidence,
            "confidence_band": _confidence_band(o.confidence),
            "title": o.title,
            "recommendation": o.recommendation,
            "evidence_ids": o.evidence_json,
            "source": _public_ai_source(o.source),
        }
        for o in rows
    ]


@router.get("/runs/{run_id}/failures")
def failures(run_id: int, db: Session = Depends(get_db)):
    run = run_or_404(db, run_id)
    targets, _ = _page_run_inputs(db, run)
    pages = (
        db.query(models.PageResult)
        .filter(models.PageResult.run_id == run_id, models.PageResult.error.is_not(None))
        .all()
    )
    output = []
    for p in pages:
        fetch_meta = ((p.structure_json or {}).get("_fetch") or {})
        output.append({
            "url": p.url,
            "role": targets[p.target_id].role,
            "status_code": p.status_code,
            "retry_count": p.retry_count,
            "terminal_state": fetch_meta.get("terminal_state"),
            "retryable": fetch_meta.get("retryable"),
            "error": p.error,
        })
    return output


@router.get("/runs/{run_id}/evidence/{evidence_id:path}")
def get_evidence(run_id: int, evidence_id: str, db: Session = Depends(get_db)):
    run = run_or_404(db, run_id)
    try:
        parts = evidence_id.split(":")
        kind = parts[0]
        row_id = int(parts[1])
    except Exception:
        raise HTTPException(400, "Evidence ID must look like page:123, density:456, section:123:4 or faq:123:2")
    if kind == "page":
        p = db.get(models.PageResult, row_id)
        if not p or p.run_id != run.id:
            raise HTTPException(404, "Evidence not found")
        return {
            "evidence_id": evidence_id,
            "type": "page",
            "url": p.url,
            "status_code": p.status_code,
            "word_count": p.word_count,
            "title": p.title,
            "meta_description": p.meta_description,
            "h1": p.h1_json,
            "h2": p.h2_json,
            "h3": p.h3_json,
            "fetched_at": p.fetched_at,
            "retry_count": p.retry_count,
            "from_cache": p.from_cache,
            "error": p.error,
            "canonical_url": p.canonical_url,
            "extraction_root": p.extraction_root,
            "paragraph_count": p.paragraph_count,
            "structure": p.structure_json or {},
        }
    if kind == "section":
        p = db.get(models.PageResult, row_id)
        if not p or p.run_id != run.id:
            raise HTTPException(404, "Evidence not found")
        try:
            order = int(parts[2])
        except Exception:
            raise HTTPException(400, "Section evidence ID must look like section:123:4")
        sections = (p.structure_json or {}).get("sections") or []
        section = next((x for x in sections if int(x.get("order") or 0) == order), None)
        if not section:
            raise HTTPException(404, "Section evidence not found")
        return {
            "evidence_id": evidence_id,
            "type": "section",
            "url": p.url,
            "page_title": p.title,
            "section": section,
        }
    if kind == "faq":
        p = db.get(models.PageResult, row_id)
        if not p or p.run_id != run.id:
            raise HTTPException(404, "Evidence not found")
        try:
            order = int(parts[2])
        except Exception:
            raise HTTPException(400, "FAQ evidence ID must look like faq:123:2")
        faqs = (p.structure_json or {}).get("faqs") or []
        faq = next((x for x in faqs if int(x.get("order") or 0) == order), None)
        if not faq:
            raise HTTPException(404, "FAQ evidence not found")
        return {"evidence_id": evidence_id, "type": "faq", "url": p.url, "faq": faq}
    if kind == "density":
        d = db.get(models.DensityResult, row_id)
        p = db.get(models.PageResult, d.page_result_id) if d else None
        if not d or not p or p.run_id != run.id:
            raise HTTPException(404, "Evidence not found")
        project_targets, project_keywords = _page_run_inputs(db, run)
        k = project_keywords.get(d.keyword_id)
        if not k:
            raise HTTPException(404, "Keyword evidence metadata not found")
        page_rows = db.query(models.PageResult).filter_by(run_id=run_id).all()
        density_rows = (
            db.query(models.DensityResult)
            .join(models.PageResult)
            .filter(models.PageResult.run_id == run_id)
            .all()
        )
        benchmark = build_density_benchmarks(page_rows, density_rows, project_targets).get(k.id, {})
        return {
            "evidence_id": evidence_id,
            "type": "density",
            "url": p.url,
            "keyword": k.keyword,
            "keyword_type": (k.keyword_type or "OTHER").upper(),
            "search_volume": k.search_volume,
            "count": d.count,
            "word_count": p.word_count,
            "density_pct": d.density_pct,
            "target_min": benchmark.get("target_min"),
            "target_max": benchmark.get("target_max"),
            "target_min_source": benchmark.get("target_min_source"),
            "target_max_source": benchmark.get("target_max_source"),
            "benchmark_state": benchmark.get("benchmark_state"),
            "other_benchmark_count": benchmark.get("other_count", 0),
            "other_benchmark_median": benchmark.get("other_median"),
            "competitor_pool_median": benchmark.get("competitor_median"),
        }
    raise HTTPException(400, "Unsupported evidence type")


@router.get("/runs/{run_id}/export.xlsx")
def export_xlsx(run_id: int, db: Session = Depends(get_db)):
    run_or_404(db, run_id)
    fileobj = build_run_workbook(db, run_id)
    return StreamingResponse(
        fileobj,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="seo-run-{run_id}.xlsx"'},
    )

# ============================================================================
# Sitemap Intelligence
# ============================================================================

def sitemap_run_dict(r):
    analysis = r.analysis_json or {}
    return {
        "id": r.id,
        "run_number": r.run_number,
        "project_id": r.project_id,
        "status": r.status,
        "phase": r.phase,
        "progress_current": r.progress_current,
        "progress_total": r.progress_total,
        "error": r.error,
        "created_at": r.created_at,
        "started_at": r.started_at,
        "completed_at": r.completed_at,
        "config": r.config_json,
        "comparison_status": analysis.get("comparison_status"),
        "source_discovery": analysis.get("source_discovery", []),
    }


@router.get("/projects/{project_id}/sitemap-sources")
def list_sitemap_sources(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    rows = (
        db.query(models.SitemapSource)
        .filter_by(project_id=project_id, enabled=True)
        .order_by(models.SitemapSource.id)
        .all()
    )
    return [
        {
            "id": x.id,
            "root_url": x.root_url,
            "role": x.role,
            "label": x.label,
            "enabled": x.enabled,
            "created_at": x.created_at,
        }
        for x in rows
    ]


@router.post(
    "/projects/{project_id}/sitemap-sources",
    status_code=201,
)
async def add_sitemap_source(
    project_id: int,
    body: schemas.SitemapSourceCreate,
    db: Session = Depends(get_db),
):
    project_or_404(
        db,
        project_id,
    )

    root_url = str(
        body.root_url
    )

    await _require_public_web_url(
        root_url,
        field_name="Sitemap URL",
    )

    if body.role == "CLIENT":
        existing_client = (
            db.query(
                models.SitemapSource
            )
            .filter_by(
                project_id=project_id,
                role="CLIENT",
                enabled=True,
            )
            .first()
        )

        if existing_client:
            raise HTTPException(
                400,
                "Only one CLIENT sitemap source "
                "is allowed per project",
            )

    existing = (
        db.query(
            models.SitemapSource
        )
        .filter_by(
            project_id=project_id,
            root_url=root_url,
        )
        .first()
    )

    if existing:
        if existing.enabled:
            raise HTTPException(
                409,
                "Sitemap source already exists "
                "in this project",
            )

        existing.enabled = True
        existing.role = body.role
        existing.label = body.label
        row = existing

    else:
        row = models.SitemapSource(
            project_id=project_id,
            root_url=root_url,
            role=body.role,
            label=body.label,
            enabled=True,
        )

        db.add(row)

    try:
        db.commit()
        db.refresh(row)

    except IntegrityError as exc:
        db.rollback()

        raise HTTPException(
            409,
            "Sitemap source already exists "
            "in this project",
        ) from exc

    return {
        "id": row.id,
        "root_url": row.root_url,
        "role": row.role,
        "label": row.label,
        "enabled": row.enabled,
    }



@router.delete("/sitemap-sources/{source_id}", status_code=204)
def delete_sitemap_source(source_id: int, db: Session = Depends(get_db)):
    row = db.get(models.SitemapSource, source_id)
    if not row:
        raise HTTPException(404, "Sitemap source not found")

    # Existing behaviour retained: historical evidence keeps its source row.
    row.enabled = False
    db.commit()


@router.get("/projects/{project_id}/sitemap-runs")
def list_sitemap_runs(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    rows = (
        db.query(models.SitemapRun)
        .filter_by(project_id=project_id)
        .order_by(models.SitemapRun.run_number.desc(), models.SitemapRun.id.desc())
        .limit(30)
        .all()
    )
    return [sitemap_run_dict(x) for x in rows]

@router.post("/projects/{project_id}/sitemap-runs", status_code=201)
async def create_sitemap_run(
    project_id: int,
    body: schemas.SitemapRunCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    project_or_404(db, project_id)
    sources = (
        db.query(models.SitemapSource)
        .filter_by(project_id=project_id, enabled=True)
        .order_by(models.SitemapSource.id)
        .all()
    )

    clients = [x for x in sources if x.role == "CLIENT"]
    competitors = [x for x in sources if x.role == "COMPETITOR"]

    if len(clients) != 1:
        raise HTTPException(400, "Configure exactly one CLIENT sitemap source")
    if not competitors:
        raise HTTPException(400, "Add at least one COMPETITOR sitemap source")

    # Validate every persisted sitemap source again when the run is requested.
    # Discovery/crawling continues to perform its own redirect-time SSRF checks.
    await asyncio.gather(
        *[
            _require_public_web_url(
                str(source.root_url),
                field_name=f"{source.role} sitemap URL",
            )
            for source in sources
        ]
    )

    try:
        enforce_project_backlog(
            db,
            "sitemap",
            project_id,
        )
    except ValueError as exc:
        raise HTTPException(
            429,
            str(exc),
        ) from exc

    config = body.model_dump()
    config["_input_snapshot"] = _sitemap_input_snapshot(sources)

    row = models.SitemapRun(
        project_id=project_id,
        run_number=_next_run_number(db, models.SitemapRun, project_id),
        status="queued",
        progress_total=1000,
        config_json=config,
        analysis_json={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    if settings.job_execution_mode == "inline":
        from app.services.sitemap_job_runner import execute_sitemap_run

        background_tasks.add_task(asyncio.run, execute_sitemap_run(row.id))
    elif settings.job_execution_mode == "cloud_run_job":
        try:
            dispatch_cloud_run_job(row.id, run_kind="sitemap")
        except Exception as exc:
            row.status = "dispatch_failed"
            row.error = f"Cloud Run Job dispatch failed: {exc}"
            db.commit()
            raise HTTPException(502, row.error)

    return sitemap_run_dict(row)


@router.post("/sitemap-runs/{run_id}/cancel")
def cancel_sitemap_run(run_id: int, db: Session = Depends(get_db)):
    row = db.get(models.SitemapRun, run_id)
    if not row:
        raise HTTPException(404, "Sitemap run not found")

    _request_cancel(row)
    db.commit()
    db.refresh(row)
    return sitemap_run_dict(row)


@router.get("/sitemap-runs/{run_id}")
def get_sitemap_run(run_id: int, db: Session = Depends(get_db)):
    row = db.get(models.SitemapRun, run_id)
    if not row: raise HTTPException(404, "Sitemap run not found")
    return sitemap_run_dict(row)


def _sitemap_analysis(run_id: int, db: Session):
    run = db.get(models.SitemapRun, run_id)
    if not run: raise HTTPException(404, "Sitemap run not found")
    return run.analysis_json or {}


@router.get("/sitemap-runs/{run_id}/overview")
def sitemap_overview(run_id: int, db: Session = Depends(get_db)):
    a = _sitemap_analysis(run_id, db); return a.get("overview", [])


@router.get("/sitemap-runs/{run_id}/url-types")
def sitemap_url_types(run_id: int, db: Session = Depends(get_db)):
    a = _sitemap_analysis(run_id, db); return a.get("url_type_summary", [])


def _filter_analysis_rows(rows,  q: str | None = None):
    out = list(rows or [])
    if q and q.strip():
        needle = q.strip().casefold()
        out = [x for x in out if needle in " ".join(str(v) for v in x.values()).casefold()]
    return out


@router.get("/sitemap-runs/{run_id}/topics")
def sitemap_topics(
    run_id: int,
    taxonomy_page: int = 1,
    cluster_page: int = 1,
    coverage_page: int = 1,
    page_size: int = 100,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    a = _sitemap_analysis(run_id, db)
    return {
        "site_profiles": a.get("site_profiles", []),
        "taxonomy": topic_taxonomy_page(db, run_id, taxonomy_page, page_size, q),
        "clusters": cluster_audit_page(db, run_id, cluster_page, page_size, q),
        "coverage": topic_coverage_page(db, run_id, coverage_page, page_size, q),
    }


@router.get("/sitemap-runs/{run_id}/head-to-head")
def sitemap_head_to_head(
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    if not db.get(models.SitemapRun, run_id):
        raise HTTPException(404, "Sitemap run not found")
    return head_to_head_page(db, run_id, page, page_size, q)


@router.get("/sitemap-runs/{run_id}/missing-categories")
def sitemap_missing_categories(
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    if not db.get(models.SitemapRun, run_id):
        raise HTTPException(404, "Sitemap run not found")
    return missing_categories_page(db, run_id, page, page_size, q)


@router.get("/sitemap-runs/{run_id}/opportunities")
def sitemap_opportunities(
    run_id: int,
    page: int = 1,
    page_size: int = 50,
    priority: str | None = None,
    gap_type: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    if not db.get(models.SitemapRun, run_id):
        raise HTTPException(404, "Sitemap run not found")
    result = opportunities_page(
        db,
        run_id,
        page=page,
        page_size=page_size,
        priority=priority,
        gap_type=gap_type,
        q=q,
    )
    for row in result.get("items", []):
        row["confidence_band"] = _confidence_band(row.get("confidence"))
        row["confidence_label"] = "evidence strength"
        row["source"] = _public_ai_source(row.get("source"))
    return result


@router.get("/sitemap-runs/{run_id}/inventory")
def sitemap_inventory(run_id: int, page: int = 1, page_size: int = 100, broad_type: str | None = None, detailed_type: str | None = None, source_id: int | None = None, q: str | None = None, db: Session = Depends(get_db)):
    run = db.get(models.SitemapRun, run_id)
    if not run: raise HTTPException(404, "Sitemap run not found")
    page = max(1, page); page_size = max(1, min(page_size, settings.sitemap_inventory_page_size_max))
    query = db.query(models.SitemapUrl).filter_by(sitemap_run_id=run_id)
    if broad_type: query = query.filter(models.SitemapUrl.broad_page_type == broad_type.upper())
    if detailed_type: query = query.filter(models.SitemapUrl.detailed_page_type == detailed_type.upper())
    if source_id: query = query.filter(models.SitemapUrl.source_id == source_id)
    if q:
        needle = q.strip()
        like = f"%{needle}%"
        source_meta = _sitemap_run_sources(db, run)
        matching_source_ids = [
            x.id
            for x in source_meta.values()
            if needle.casefold() in str(x.label or "").casefold()
            or needle.casefold() in str(x.role or "").casefold()
        ]
        from sqlalchemy import or_
        clauses = [
            models.SitemapUrl.normalized_url.ilike(like),
            models.SitemapUrl.original_url.ilike(like),
            models.SitemapUrl.final_url.ilike(like),
            models.SitemapUrl.canonical_url.ilike(like),
            models.SitemapUrl.title.ilike(like),
            models.SitemapUrl.category.ilike(like),
            models.SitemapUrl.topic.ilike(like),
            models.SitemapUrl.subtopic.ilike(like),
        ]
        if matching_source_ids:
            clauses.append(models.SitemapUrl.source_id.in_(matching_source_ids))
        query = query.filter(or_(*clauses))
    total = query.count()
    rows = query.order_by(models.SitemapUrl.id).offset((page-1)*page_size).limit(page_size).all()
    sources = _sitemap_run_sources(db, run)
    return {"page": page, "page_size": page_size, "total": total, "items": [{
        "id": x.id, "evidence_id": f"sitemap-url:{x.id}", "site": sources[x.source_id].label if x.source_id in sources else x.source_id,
        "role": sources[x.source_id].role if x.source_id in sources else "", "url": x.original_url,
        "normalized_url": x.normalized_url, "final_url": x.final_url, "canonical_url": x.canonical_url,
        "root_sitemap": x.root_sitemap, "child_sitemap": x.child_sitemap,
        "http_status": x.http_status, "broad_page_type": x.broad_page_type,
        "detailed_page_type": x.detailed_page_type, "type_confidence": x.type_confidence, "type_confidence_band": _confidence_band(x.type_confidence), "classification_method": x.classification_method,
        "category": x.category, "topic": x.topic, "subtopic": x.subtopic, "topic_confidence": x.topic_confidence, "topic_confidence_band": _confidence_band(x.topic_confidence), "title": x.title, "h1": x.h1_json,
        "word_count": x.word_count, "lastmod": x.lastmod, "fetch_method": x.fetch_method, "error": x.error,
    } for x in rows]}



@router.get("/sitemap-runs/{run_id}/duplicates")
def sitemap_duplicates(
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    duplicate_kind: str | None = None,
    source_id: int | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    if not db.get(models.SitemapRun, run_id):
        raise HTTPException(404, "Sitemap run not found")
    return duplicate_audit_page(
        db,
        run_id,
        page=page,
        page_size=min(max(1, page_size), settings.sitemap_inventory_page_size_max),
        duplicate_kind=duplicate_kind,
        source_id=source_id,
        q=q,
    )


@router.get("/sitemap-runs/{run_id}/canonicals")
def sitemap_canonicals(
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    source_id: int | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    if not db.get(models.SitemapRun, run_id):
        raise HTTPException(404, "Sitemap run not found")
    return canonical_audit_page(
        db,
        run_id,
        page=page,
        page_size=min(max(1, page_size), settings.sitemap_inventory_page_size_max),
        source_id=source_id,
        q=q,
    )


@router.get("/sitemap-runs/{run_id}/failures")
def sitemap_failures(
    run_id: int,
    page: int = 1,
    page_size: int = 100,
    http_status: int | None = None,
    host: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    if not db.get(models.SitemapRun, run_id):
        raise HTTPException(404, "Sitemap run not found")
    return failure_page(
        db,
        run_id,
        page=page,
        page_size=min(max(1, page_size), settings.sitemap_failure_page_size_max),
        http_status=http_status,
        host=host,
        q=q,
    )


@router.post("/sitemap-runs/{run_id}/recover-failures")
def recover_sitemap_failures(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.SitemapRun, run_id)
    if not run:
        raise HTTPException(404, "Sitemap run not found")
    if run.status in _LIVE_RUN_STATUSES:
        raise HTTPException(409, "This sitemap run is already active")

    failed_count = (
        db.query(func.count(models.SitemapUrl.id))
        .filter(
            models.SitemapUrl.sitemap_run_id == run_id,
            models.SitemapUrl.error.is_not(None),
        )
        .scalar()
        or 0
    )
    if not failed_count:
        raise HTTPException(409, "This sitemap run has no persisted failed URLs to recover")

    cfg = dict(run.config_json or {})
    requested_at = _utcnow().isoformat()

    # Preserve each host's learned-safe request interval from the previous
    # recovery session. Re-running recovery on the SAME persisted run should not
    # forget everything it learned and immediately return to an aggressive rate.
    previous_state = dict(cfg.get("_recovery_state") or {})
    learned_hosts = dict(previous_state.get("learned_hosts") or {})
    for host, prior in dict(previous_state.get("hosts") or {}).items():
        prior = dict(prior or {})
        learned = dict(learned_hosts.get(host) or {})
        safe = (
            prior.get("last_known_safe_interval_seconds")
            or prior.get("bulk_interval_seconds")
        )
        if safe is not None:
            learned["last_known_safe_interval_seconds"] = safe
        learned_hosts[host] = learned

    cfg["_recovery_only"] = {
        "requested_at": requested_at,
        "failed_rows_at_request": int(failed_count),
        "mode": "failed_urls_only",
        "controller": "adaptive-learned-rate-v2",
    }
    cfg["_recovery_state"] = {
        "version": 2,
        "status": "queued",
        "requested_at": requested_at,
        "hosts": {},
        "learned_hosts": learned_hosts,
        "attempted_total": 0,
        "recovered_total": 0,
    }
    control = dict(cfg.get("_control") or {})
    control.update({
        "cancel_requested": False,
        "cancel_requested_at": None,
        "recovery_requested": True,
        "recovery_requested_at": requested_at,
        "recovery_completed_at": None,
        "recovery_terminal_status": None,
    })
    cfg["_control"] = control
    cfg.pop("_progress", None)

    run.config_json = cfg
    run.status = "queued"
    run.phase = "recovery_queued"
    run.error = None
    run.started_at = None
    run.completed_at = None
    run.progress_current = 0
    run.progress_total = 1000
    db.commit()
    db.refresh(run)
    return sitemap_run_dict(run)


@router.get("/sitemap-runs/{run_id}/evidence/{url_id}")
def sitemap_evidence(run_id: int, url_id: int, db: Session = Depends(get_db)):
    row = db.get(models.SitemapUrl, url_id)
    if not row or row.sitemap_run_id != run_id:
        raise HTTPException(404, "Sitemap evidence not found")
    run = db.get(models.SitemapRun, run_id)
    source = _sitemap_run_sources(db, run).get(row.source_id) if run else None
    return {
        "evidence_id": f"sitemap-url:{row.id}", "site": source.label if source else row.source_id, "role": source.role if source else "",
        "root_sitemap": row.root_sitemap, "child_sitemap": row.child_sitemap, "original_url": row.original_url,
        "normalized_url": row.normalized_url, "final_url": row.final_url, "canonical_url": row.canonical_url, "http_status": row.http_status,
        "page_type": row.detailed_page_type, "broad_page_type": row.broad_page_type, "type_confidence": row.type_confidence,
        "classification_method": row.classification_method, "classification_evidence": row.classification_evidence_json,
        "category": row.category, "topic": row.topic, "subtopic": row.subtopic, "topic_confidence": row.topic_confidence,
        "title": row.title, "meta_description": row.meta_description, "h1": row.h1_json, "h2": row.h2_json,
        "schema_types": row.schema_types_json, "breadcrumbs": row.breadcrumbs_json, "word_count": row.word_count,
        "author_present": row.author_present, "published_at": row.published_at_text, "cta_signals": row.cta_json,
        "text_excerpt": row.text_excerpt, "lastmod": row.lastmod, "fetch_method": row.fetch_method, "error": row.error,
        "terminal_state": (row.classification_evidence_json or {}).get("terminal_state"),
    }


@router.get("/sitemap-runs/{run_id}/export.xlsx")
def sitemap_export_xlsx(run_id: int, db: Session = Depends(get_db)):
    if not db.get(models.SitemapRun, run_id): raise HTTPException(404, "Sitemap run not found")
    from app.services.sitemap_export import build_sitemap_workbook
    fileobj = build_sitemap_workbook(db, run_id)
    return StreamingResponse(fileobj, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="sitemap-intelligence-{run_id}.xlsx"'})
