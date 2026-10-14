from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.ai import refine_with_ai
from app.services.content_analysis import (
    apply_section_ai_reviews,
    build_ai_granular_context,
    build_content_opportunity_rows,
    build_detailed_analysis,
)
from app.services.crawler import Crawler, PAGE_STRUCTURE_VERSION
from app.services.opportunities import build_opportunities
from app.services.text import BatchKeywordAnalyzer

settings = get_settings()


class RunCancellationRequested(Exception):
    pass


def _external_cancel_requested(run_id: int) -> bool:
    """Read cancellation from a separate session so inline/background execution
    can observe a control request made by another API request immediately."""
    control_db = SessionLocal()
    try:
        row = control_db.get(models.Run, run_id)
        if not row:
            return True
        if str(row.status or "").strip().casefold() in {"cancelling", "cancelled"}:
            return True
        control = dict((row.config_json or {}).get("_control") or {})
        return bool(control.get("cancel_requested"))
    finally:
        control_db.close()


# Overall progress is stored in the existing Run.progress_current/total columns
# using a 0..1000 scale, so no DB migration is required. Rich progress metadata
# lives under Run.config_json["_progress"].
_PROGRESS_SCALE = 1000
_LAST_PROGRESS_WRITE: dict[int, datetime] = {}


def _progress_plan(ai_enabled: bool) -> dict[str, tuple[float, float, str]]:
    if ai_enabled:
        return {
            "initializing": (0.0, 2.0, "Preparing analysis"),
            "crawl": (2.0, 34.0, "Crawling and extracting pages"),
            "phase1_density": (34.0, 48.0, "Calculating keyword density"),
            "phase2_opportunities": (
                48.0,
                56.0,
                "Building deterministic opportunities",
            ),
            "phase2_detailed_analysis": (
                56.0,
                78.0,
                "Comparing sections, keywords and FAQs",
            ),
            "ai_deep_analysis": (78.0, 97.0, "AI model deep section analysis"),
            "finalizing": (97.0, 99.5, "Finalizing report data"),
            "complete": (100.0, 100.0, "Analysis complete"),
            "failed": (100.0, 100.0, "Analysis failed"),
        }

    return {
        "initializing": (0.0, 2.0, "Preparing analysis"),
        "crawl": (2.0, 40.0, "Crawling and extracting pages"),
        "phase1_density": (40.0, 58.0, "Calculating keyword density"),
        "phase2_opportunities": (
            58.0,
            68.0,
            "Building deterministic opportunities",
        ),
        "phase2_detailed_analysis": (
            68.0,
            96.0,
            "Comparing sections, keywords and FAQs",
        ),
        "finalizing": (96.0, 99.5, "Finalizing report data"),
        "complete": (100.0, 100.0, "Analysis complete"),
        "failed": (100.0, 100.0, "Analysis failed"),
    }


def _set_progress(
    db: Session,
    run,
    phase: str,
    fraction: float = 0.0,
    *,
    message: str | None = None,
    stage_current: int | None = None,
    stage_total: int | None = None,
    force: bool = False,
):
    """Persist weighted run progress, ETA and heartbeat without new columns."""
    if _external_cancel_requested(run.id):
        raise RunCancellationRequested(f"Run {run.id} cancellation requested")

    now = datetime.now(timezone.utc)
    cfg = dict(run.config_json or {})
    ai_enabled = bool(cfg.get("enable_ai", True)) and settings.ai_mode != "off"
    plan = _progress_plan(ai_enabled)
    start, end, label = plan.get(
        phase,
        (0.0, 99.0, phase.replace("_", " ")),
    )
    fraction = max(0.0, min(1.0, float(fraction)))
    percent = 100.0 if phase == "complete" else start + (end - start) * fraction

    old_meta = dict(cfg.get("_progress") or {})
    old_percent = float(old_meta.get("percent") or 0.0)

    # Never move the visible progress bar backwards during a run.
    if run.status not in {"failed", "dispatch_failed"}:
        percent = max(old_percent, percent)

    started = run.started_at
    if started and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0

    estimated_total = old_meta.get("estimated_total_seconds")

    # Re-estimate only when measurable progress actually advances. This prevents
    # the ETA jumping merely because a single AI-model request is taking time.
    if percent > old_percent + 0.05 and percent >= 3.0 and elapsed >= 1.0:
        sample_total = elapsed / max(percent / 100.0, 0.01)
        if estimated_total:
            estimated_total = 0.70 * float(estimated_total) + 0.30 * sample_total
        else:
            estimated_total = sample_total

    eta_seconds = None
    if estimated_total and percent < 100:
        eta_seconds = max(
            0,
            min(int(float(estimated_total) - elapsed), 24 * 3600),
        )

    meta = {
        "percent": round(percent, 1),
        "stage_label": label,
        "message": message or label,
        "stage_current": stage_current,
        "stage_total": stage_total,
        "elapsed_seconds": int(elapsed),
        "eta_seconds": eta_seconds,
        "estimated_total_seconds": (
            round(float(estimated_total), 1) if estimated_total else None
        ),
        "heartbeat_at": now.isoformat(),
    }

    cfg["_progress"] = meta
    run.config_json = cfg
    run.phase = phase
    run.progress_current = int(round(percent / 100.0 * _PROGRESS_SCALE))
    run.progress_total = _PROGRESS_SCALE

    last = _LAST_PROGRESS_WRITE.get(run.id)
    should_write = (
        force
        or last is None
        or (now - last).total_seconds() >= 0.75
        or percent >= 99.5
    )

    if should_write:
        db.commit()
        _LAST_PROGRESS_WRITE[run.id] = now


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()


def _cache_payload_is_healthy(payload: dict) -> bool:
    """Return True only for reusable Page Analysis cache payloads.

    The important reliability rule is that HTTP success alone is not sufficient.
    A 200 response with zero extracted words/body text is a poisoned cache entry and
    must be crawled again.
    """
    if not isinstance(payload, dict):
        return False

    if int(payload.get("structure_version") or 0) < PAGE_STRUCTURE_VERSION:
        return False

    if payload.get("error"):
        return False

    try:
        status = int(payload.get("status_code") or 0)
    except (TypeError, ValueError):
        return False

    if status < 200 or status >= 400:
        return False

    try:
        word_count = int(payload.get("word_count") or 0)
    except (TypeError, ValueError):
        return False

    body_text = str(payload.get("body_text") or "").strip()
    if word_count <= 0 or not body_text:
        return False

    structure = payload.get("structure") or {}
    fetch_meta = structure.get("_fetch") or {}

    terminal_state = str(fetch_meta.get("terminal_state") or "usable").strip()
    if terminal_state != "usable":
        return False

    heading_count = sum(
        len(structure.get(level) or []) for level in ("h1", "h2", "h3")
    )

    try:
        paragraph_count = int(
            payload.get("paragraph_count")
            or structure.get("paragraph_count")
            or 0
        )
    except (TypeError, ValueError):
        paragraph_count = 0

    # Extremely thin content may be legitimate, so only reject the combination of
    # very little text AND no structural evidence at all.
    if word_count < 40 and heading_count == 0 and paragraph_count == 0:
        return False

    return True


def _cache_get(db: Session, url: str):
    row = (
        db.query(models.PageCache)
        .filter_by(url_hash=_cache_key(url))
        .first()
    )

    if not row:
        return None

    now = datetime.now(timezone.utc)
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if expires <= now:
        return None

    payload = row.payload_json or {}

    # Structure V4 changes root selection and fetch-health semantics.  Older entries,
    # including the previous "HTTP 200 + zero extracted words" case, are automatically
    # recrawled once and then replaced with a healthy V4 payload.
    if not _cache_payload_is_healthy(payload):
        db.delete(row)
        db.flush()
        return None

    return payload


def _cache_put(db: Session, url: str, payload: dict):
    # Failed/empty/challenge responses must never poison future analysis runs.
    if not url or not _cache_payload_is_healthy(payload):
        return

    key = _cache_key(url)
    row = db.query(models.PageCache).filter_by(url_hash=key).first()
    now = datetime.now(timezone.utc)

    values = dict(
        url=url,
        payload_json=payload,
        content_hash=payload.get("content_hash"),
        stored_at=now,
        expires_at=now + timedelta(hours=settings.cache_ttl_hours),
    )

    if row:
        for key_name, value in values.items():
            setattr(row, key_name, value)
    else:
        db.add(models.PageCache(url_hash=key, **values))


def _save_page(
    db: Session,
    run_id: int,
    target_id: int,
    payload: dict,
    from_cache: bool,
):
    page = models.PageResult(
        run_id=run_id,
        target_id=target_id,
        url=payload.get("url") or "",
        final_url=payload.get("final_url"),
        status_code=payload.get("status_code"),
        fetched_at=_parse_dt(payload.get("fetched_at")),
        retry_count=int(payload.get("retry_count") or 0),
        from_cache=from_cache,
        word_count=int(payload.get("word_count") or 0),
        title=payload.get("title"),
        meta_description=payload.get("meta_description"),
        h1_json=payload.get("h1") or [],
        h2_json=payload.get("h2") or [],
        h3_json=payload.get("h3") or [],
        body_text=payload.get("body_text") or "",
        content_hash=payload.get("content_hash"),
        error=payload.get("error"),
        canonical_url=payload.get("canonical_url"),
        extraction_root=payload.get("extraction_root"),
        paragraph_count=int(payload.get("paragraph_count") or 0),
        structure_json=payload.get("structure") or {},
    )
    db.add(page)
    db.flush()
    return page


def _normalize_key(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^a-z0-9]+", " ", (value or "").lower()),
    ).strip()


def _merge_ai_recommendation(base: str, review: dict) -> str:
    """One recommendation row: deterministic fact + AI-model depth review."""
    pieces = []

    ai_rec = (review.get("recommendation") or "").strip()
    if ai_rec:
        pieces.append(ai_rec)

    missing = [
        str(item).strip()
        for item in (review.get("missing_aspects") or [])
        if str(item).strip()
    ]
    if missing:
        pieces.append("Missing/under-covered aspects: " + "; ".join(missing))

    coverage = str(review.get("coverage_assessment") or "").strip()
    relevance = str(review.get("intent_relevance") or "").strip()
    action = str(review.get("recommended_action") or "").strip()

    meta = []
    if coverage:
        meta.append(f"coverage={coverage}")
    if relevance:
        meta.append(f"intent relevance={relevance}")
    if action:
        meta.append(f"action={action}")

    if meta:
        pieces.append("AI model deep review: " + ", ".join(meta) + ".")

    # Keep deterministic recommendation as traceable fallback/context, but do not
    # create a second database opportunity row.
    if base and base.strip() and base.strip() not in pieces:
        pieces.append("Deterministic evidence note: " + base.strip())

    return " ".join(pieces).strip()


def _ai_review_needed(snapshot: dict, deterministic: list[dict]) -> bool:
    """Gate generative reasoning to material or ambiguous cases."""
    allowed = {
        item.strip().upper()
        for item in settings.ai_review_priorities.split(",")
        if item.strip()
    }

    if any(
        str(item.get("priority", "")).upper() in allowed
        for item in deterministic
    ):
        return True

    for row in snapshot.get("section_comparison", []) or []:
        priority = str(row.get("Opportunity Priority") or "").upper()
        if priority in allowed:
            return True

        try:
            similarity = float(row.get("Client Topic Similarity") or 0)
        except (TypeError, ValueError):
            similarity = 0.0

        if (
            settings.ai_review_ambiguous_similarity_low
            <= similarity
            <= settings.ai_review_ambiguous_similarity_high
        ):
            return True

    if any(
        str(row.get("Opportunity Priority") or "")
        not in {"No Change", "No Placement Change", ""}
        for row in snapshot.get("keyword_placement", []) or []
    ):
        return True

    if any(
        str(row.get("Opportunity Priority") or "") not in {"No Change", ""}
        for row in snapshot.get("faq_comparison", []) or []
    ):
        return True

    return False


async def execute_run(run_id: int):
    db = SessionLocal()

    try:
        run = db.get(models.Run, run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        if run.status in {"completed", "cancelled", "cancelling"}:
            return

        project = db.get(models.Project, run.project_id)
        run_cfg = dict(run.config_json or {})
        snapshot = dict(run_cfg.get("_input_snapshot") or {})
        target_snapshot = list(snapshot.get("targets") or [])
        keyword_snapshot = list(snapshot.get("keywords") or [])

        if target_snapshot:
            targets = [
                SimpleNamespace(**item)
                for item in target_snapshot
                if item.get("id") is not None
            ]
        else:
            # Backward compatibility for pre-snapshot runs.
            targets = (
                db.query(models.Target)
                .filter_by(project_id=project.id, enabled=True)
                .order_by(models.Target.id)
                .all()
            )

        if keyword_snapshot:
            keywords = [
                SimpleNamespace(**item)
                for item in keyword_snapshot
                if item.get("id") is not None
            ]
        else:
            # Backward compatibility for pre-snapshot runs.
            keywords = (
                db.query(models.Keyword)
                .filter_by(project_id=project.id, enabled=True)
                .order_by(models.Keyword.id)
                .all()
            )

        if not targets:
            raise ValueError("Run snapshot has no target URLs")
        if not keywords:
            raise ValueError("Run snapshot has no keywords")

        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.progress_current = 0
        run.progress_total = _PROGRESS_SCALE
        run.error = None
        run.analysis_json = {}

        _set_progress(
            db,
            run,
            "initializing",
            0.25,
            message="Preparing run, clearing previous temporary results",
            force=True,
        )

        db.execute(
            delete(models.Opportunity).where(models.Opportunity.run_id == run_id)
        )

        old_pages = db.query(models.PageResult).filter_by(run_id=run_id).all()
        old_page_ids = [page.id for page in old_pages]

        if old_page_ids:
            db.execute(
                delete(models.DensityResult).where(
                    models.DensityResult.page_result_id.in_(old_page_ids)
                )
            )

        db.execute(
            delete(models.PageResult).where(models.PageResult.run_id == run_id)
        )
        db.commit()

        run_cfg = dict(run.config_json or {})
        use_cache = bool(run_cfg.get("use_cache", True)) and not bool(
            run_cfg.get("force_refresh", False)
        )

        pending = []
        saved_pages = []
        cached_count = 0
        total_targets = len(targets)

        _set_progress(
            db,
            run,
            "crawl",
            0.0,
            message=f"Checking cache and preparing {total_targets} URL(s)",
            stage_current=0,
            stage_total=total_targets,
            force=True,
        )

        for target in targets:
            cached = _cache_get(db, target.url) if use_cache else None

            if cached:
                page = _save_page(db, run_id, target.id, cached, True)
                saved_pages.append(page)
                cached_count += 1

                _set_progress(
                    db,
                    run,
                    "crawl",
                    cached_count / max(1, total_targets),
                    message=(
                        f"Loaded {cached_count}/{total_targets} page(s) from cache"
                    ),
                    stage_current=cached_count,
                    stage_total=total_targets,
                )
            else:
                pending.append((target.id, target.url))

        db.commit()

        if pending:
            crawler = Crawler()
            crawl_concurrency = (
                run_cfg.get("crawl_concurrency") or settings.crawl_concurrency
            )

            async def crawl_progress(
                target_id,
                payload,
                completed,
                pending_total,
            ):
                overall_done = cached_count + completed
                _set_progress(
                    db,
                    run,
                    "crawl",
                    overall_done / max(1, total_targets),
                    message=(
                        f"Crawled {overall_done}/{total_targets}: "
                        f"{payload.get('url') or target_id}"
                    ),
                    stage_current=overall_done,
                    stage_total=total_targets,
                    force=True,
                )

            results = await crawler.crawl_many(
                pending,
                crawl_concurrency,
                on_result=crawl_progress,
            )

            for target_id, payload in results:
                page = _save_page(db, run_id, target_id, payload, False)
                saved_pages.append(page)

                # _cache_put performs the final health gate.  Failed/challenge/empty
                # payloads are intentionally not cached.
                _cache_put(db, payload.get("url") or "", payload)

            db.commit()

        _set_progress(
            db,
            run,
            "crawl",
            1.0,
            message=(
                f"Crawl complete: {total_targets}/{total_targets} URL(s) processed"
            ),
            stage_current=total_targets,
            stage_total=total_targets,
            force=True,
        )

        # ------------------------------------------------------------------
        # PHASE 1: optimized exact density
        # ------------------------------------------------------------------
        pages = db.query(models.PageResult).filter_by(run_id=run_id).all()
        batch_analyzer = BatchKeywordAnalyzer(
            (keyword.id, keyword.keyword) for keyword in keywords
        )
        page_total = max(1, len(pages))

        _set_progress(
            db,
            run,
            "phase1_density",
            0.0,
            message=(
                f"Preparing {len(keywords)} keyword(s) across {len(pages)} page(s)"
            ),
            stage_current=0,
            stage_total=len(pages),
            force=True,
        )

        for page_index, page in enumerate(pages, start=1):
            # A failed crawl is unavailable; never manufacture 0.00% rows.
            if not page.error and page.word_count > 0:
                analyzed = batch_analyzer.analyze(page.body_text or "")

                for keyword in keywords:
                    count, density, total_words = analyzed[keyword.id]

                    # Keep persisted PageResult.word_count consistent with the exact
                    # tokenizer used by the density calculation.
                    if total_words and page.word_count != total_words:
                        page.word_count = total_words

                    db.add(
                        models.DensityResult(
                            page_result_id=page.id,
                            keyword_id=keyword.id,
                            # HISTORICAL FORMULA:
                            # occurrences / total words * 100
                            density_pct=density,
                            count=count,
                            present=count > 0,
                        )
                    )

            _set_progress(
                db,
                run,
                "phase1_density",
                page_index / page_total,
                message=(
                    f"Density analyzed for page {page_index}/{len(pages)}"
                ),
                stage_current=page_index,
                stage_total=len(pages),
            )

        db.commit()

        _set_progress(
            db,
            run,
            "phase1_density",
            1.0,
            message=(
                f"Density complete: {len(pages) * len(keywords):,} keyword-page checks"
            ),
            stage_current=len(pages),
            stage_total=len(pages),
            force=True,
        )

        # ------------------------------------------------------------------
        # PHASE 2: deterministic + semantic granular analysis
        # ------------------------------------------------------------------
        _set_progress(
            db,
            run,
            "phase2_opportunities",
            0.05,
            message="Building deterministic keyword and competitor gaps",
            force=True,
        )

        target_map = {target.id: target for target in targets}
        successful = [page for page in pages if not page.error]

        client_pages = [
            page
            for page in successful
            if target_map[page.target_id].role.upper() == "CLIENT"
        ]
        client_page = client_pages[0] if client_pages else None

        competitor_pages = [
            page
            for page in successful
            if target_map[page.target_id].role.upper() != "CLIENT"
        ]

        densities = (
            db.query(models.DensityResult)
            .join(models.PageResult)
            .filter(models.PageResult.run_id == run_id)
            .all()
        )
        dmap = {
            (density.page_result_id, density.keyword_id): density
            for density in densities
        }
        role_by_page_id = {
            page.id: target_map[page.target_id].role
            for page in pages
        }

        deterministic, evidence_index = build_opportunities(
            client_page,
            competitor_pages,
            keywords,
            dmap,
            role_by_page_id,
        )

        _set_progress(
            db,
            run,
            "phase2_opportunities",
            0.65,
            message=(
                f"Generated {len(deterministic)} deterministic opportunity candidate(s)"
            ),
            force=True,
        )

        # Assign stable per-run keys so AI model can REVIEW the same opportunity
        # instead of creating a duplicate copy.
        opportunity_rows_by_key: dict[str, int] = {}

        for index, item in enumerate(deterministic, start=1):
            key = f"DET-{index:03d}"
            item["opportunity_key"] = key

            row = models.Opportunity(
                run_id=run_id,
                category=item["category"],
                priority=item["priority"],
                confidence=item["confidence"],
                title=item["title"],
                recommendation=item["recommendation"],
                evidence_json=item["evidence_ids"],
                source="deterministic",
            )
            db.add(row)
            db.flush()
            opportunity_rows_by_key[key] = row.id

        db.commit()

        _set_progress(
            db,
            run,
            "phase2_opportunities",
            1.0,
            message=(
                f"Saved {len(deterministic)} deterministic opportunity candidate(s)"
            ),
            force=True,
        )

        _set_progress(
            db,
            run,
            "phase2_detailed_analysis",
            0.0,
            message="Starting section, keyword-placement and FAQ comparison",
            force=True,
        )

        def detailed_progress(fraction, message, current, total):
            _set_progress(
                db,
                run,
                "phase2_detailed_analysis",
                fraction,
                message=message,
                stage_current=current,
                stage_total=total,
            )

        snapshot = build_detailed_analysis(
            pages=pages,
            targets_by_id=target_map,
            keywords=keywords,
            db_opportunities=(
                db.query(models.Opportunity).filter_by(run_id=run_id).all()
            ),
            progress=detailed_progress,
        )
        run.analysis_json = snapshot
        db.commit()

        _set_progress(
            db,
            run,
            "phase2_detailed_analysis",
            1.0,
            message="Granular section/keyword/FAQ analysis complete",
            force=True,
        )

        # ------------------------------------------------------------------
        # AI MODEL DEEP ANALYSIS
        # ------------------------------------------------------------------
        run_cfg = dict(run.config_json or {})
        ai_enabled_for_run = (
            run_cfg.get("enable_ai", True)
            and settings.ai_mode != "off"
            and _ai_review_needed(snapshot, deterministic)
        )

        if ai_enabled_for_run:
            _set_progress(
                db,
                run,
                "ai_deep_analysis",
                0.05,
                message=(
                    "Preparing granular Client/P0/P1 evidence for the AI model"
                ),
                force=True,
            )

            granular, full_evidence_index = build_ai_granular_context(
                pages=pages,
                targets_by_id=target_map,
                keywords=keywords,
                detailed_analysis=snapshot,
                evidence_index=evidence_index,
            )

            allowed_ai_priorities = {
                item.strip().upper()
                for item in settings.ai_review_priorities.split(",")
                if item.strip()
            }

            ai_deterministic = [
                item
                for item in deterministic
                if str(item.get("priority", "")).upper() in allowed_ai_priorities
            ]

            payload = {
                "requested_max_opportunities": settings.ai_max_opportunities,
                "prompt_version": settings.ai_prompt_version,
                "schema_version": settings.ai_schema_version,
                "deterministic_opportunities": ai_deterministic,
                "granular_analysis": granular,
                "evidence_index": full_evidence_index,
            }

            _set_progress(
                db,
                run,
                "ai_deep_analysis",
                0.18,
                message=(
                    "AI model deep reasoning is running over full section evidence"
                ),
                force=True,
            )

            ai_task = asyncio.create_task(
                asyncio.to_thread(refine_with_ai, payload)
            )

            while not ai_task.done():
                done, _ = await asyncio.wait({ai_task}, timeout=3.0)
                if ai_task not in done:
                    # Heartbeat keeps the UI visibly alive during one long model call.
                    _set_progress(
                        db,
                        run,
                        "ai_deep_analysis",
                        0.18,
                        message=(
                            "AI model deep reasoning is active — comparing section "
                            "depth and missing aspects"
                        ),
                        force=True,
                    )

            ai_result = await ai_task

            _set_progress(
                db,
                run,
                "ai_deep_analysis",
                0.70,
                message=(
                    "AI model response received; merging reviews with deterministic "
                    "evidence"
                ),
                force=True,
            )

            # 1) REVIEW existing deterministic opportunities IN PLACE.
            # No second AI-model copy is inserted.
            for review in ai_result.get("opportunity_reviews", []):
                key = review.get("opportunity_key")
                row_id = opportunity_rows_by_key.get(key)
                if not row_id:
                    continue

                row = db.get(models.Opportunity, row_id)
                if not row:
                    continue

                base_conf = float(row.confidence or 0)
                ai_conf = float(review.get("confidence", 0))

                # Existing project behavior retained: this field is an internal
                # evidence-strength heuristic, not a validated accuracy probability.
                row.confidence = round((base_conf + ai_conf) / 2.0, 3)
                row.recommendation = _merge_ai_recommendation(
                    row.recommendation,
                    review,
                )
                row.evidence_json = list(
                    dict.fromkeys(
                        [
                            *(row.evidence_json or []),
                            *(review.get("evidence_ids") or []),
                        ]
                    )
                )
                row.source = (
                    "deterministic+ai-reviewed"
                    if settings.ai_mode == "vertex"
                    else "deterministic+mock-reviewed"
                )

            _set_progress(
                db,
                run,
                "ai_deep_analysis",
                0.82,
                message=(
                    f"Merged {len(ai_result.get('opportunity_reviews', []))} "
                    "AI-model opportunity review(s)"
                ),
                force=True,
            )

            # 2) Allow genuinely NEW deep/aspect gaps only when the AI model cites
            # valid granular evidence. Deduplicate against existing category/title.
            existing_rows = (
                db.query(models.Opportunity).filter_by(run_id=run_id).all()
            )
            existing = {
                (_normalize_key(row.category), _normalize_key(row.title))
                for row in existing_rows
            }

            existing_evidence_by_category: dict[str, list[set[str]]] = {}
            for existing_row in existing_rows:
                category_key = _normalize_key(existing_row.category)
                existing_evidence_by_category.setdefault(
                    category_key,
                    [],
                ).append(set(existing_row.evidence_json or []))

            for item in ai_result.get("new_opportunities", []):
                category_key = _normalize_key(
                    item.get("category", "ai_depth_gap")
                )
                title_key = _normalize_key(item.get("title", ""))
                key = (category_key, title_key)
                new_evidence = set(item.get("evidence_ids") or [])

                evidence_duplicate = any(
                    bool(new_evidence & old_ids)
                    for old_ids in existing_evidence_by_category.get(
                        category_key,
                        [],
                    )
                )

                # Exact category/title duplicate OR same-category recommendation
                # grounded in the same evidence is a refinement, not a new row.
                if key in existing or evidence_duplicate or not title_key:
                    continue

                missing = "; ".join(item.get("missing_aspects") or [])
                recommendation = (item.get("recommendation") or "").strip()

                if missing:
                    recommendation += (
                        (" " if recommendation else "")
                        + "Missing/under-covered aspects: "
                        + missing
                    )

                db.add(
                    models.Opportunity(
                        run_id=run_id,
                        category=item.get("category", "ai_depth_gap")[:80],
                        priority=item.get("priority", "MEDIUM"),
                        confidence=float(item.get("confidence", 0.5)),
                        title=item.get(
                            "title",
                            "AI depth opportunity",
                        )[:500],
                        recommendation=recommendation,
                        evidence_json=item.get("evidence_ids", []),
                        source=(
                            "ai-model-new"
                            if settings.ai_mode == "vertex"
                            else "mock-new"
                        ),
                    )
                )
                existing.add(key)
                existing_evidence_by_category.setdefault(
                    category_key,
                    [],
                ).append(new_evidence)

            db.commit()

            _set_progress(
                db,
                run,
                "ai_deep_analysis",
                0.92,
                message=(
                    f"Processed {len(ai_result.get('new_opportunities', []))} "
                    "AI-model depth-gap candidate(s)"
                ),
                force=True,
            )

            # 3) Put section-level AI-model depth/aspect reasoning directly into the
            # granular Section Comparison snapshot, then rebuild the consolidated
            # opportunity sheet from the deduplicated DB rows.
            snapshot = apply_section_ai_reviews(
                dict(run.analysis_json or {}),
                ai_result.get("section_reviews", []),
            )
            snapshot["ai_opportunity_reviews"] = ai_result.get(
                "opportunity_reviews",
                [],
            )
            snapshot["content_opportunities"] = build_content_opportunity_rows(
                snapshot.get("section_comparison", []),
                snapshot.get("keyword_placement", []),
                snapshot.get("faq_comparison", []),
                db.query(models.Opportunity).filter_by(run_id=run_id).all(),
            )
            run.analysis_json = snapshot
            db.commit()

            _set_progress(
                db,
                run,
                "ai_deep_analysis",
                1.0,
                message="AI model deep analysis merged and validated",
                force=True,
            )

        run_cfg = dict(run.config_json or {})
        if (
            run_cfg.get("enable_ai", True)
            and settings.ai_mode != "off"
            and not ai_enabled_for_run
        ):
            _set_progress(
                db,
                run,
                "ai_deep_analysis",
                1.0,
                message=(
                    "AI review skipped: deterministic/semantic evidence was already "
                    "decisive"
                ),
                force=True,
            )

        _set_progress(
            db,
            run,
            "finalizing",
            0.55,
            message="Saving final analysis snapshot and preparing export data",
            force=True,
        )

        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)

        _set_progress(
            db,
            run,
            "complete",
            1.0,
            message="Analysis complete — results and Excel export are ready",
            force=True,
        )

    except RunCancellationRequested:
        run = db.get(models.Run, run_id)
        if run:
            run.status = "cancelled"
            run.phase = "cancelled"
            run.error = None
            run.completed_at = datetime.now(timezone.utc)
            cfg = dict(run.config_json or {})
            control = dict(cfg.get("_control") or {})
            control.update(
                {
                    "cancel_requested": True,
                    "cancelled_at": datetime.now(timezone.utc).isoformat(),
                    "cancel_state": "cooperative",
                }
            )
            cfg["_control"] = control
            run.config_json = cfg
            db.commit()
        return

    except Exception as exc:
        run = db.get(models.Run, run_id)
        if run:
            run.status = "failed"
            run.phase = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.completed_at = datetime.now(timezone.utc)

            cfg = dict(run.config_json or {})
            meta = dict(cfg.get("_progress") or {})
            meta.update(
                {
                    "stage_label": "Analysis failed",
                    "message": run.error,
                    "eta_seconds": None,
                    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            cfg["_progress"] = meta
            run.config_json = cfg
            db.commit()

        raise

    finally:
        db.close()