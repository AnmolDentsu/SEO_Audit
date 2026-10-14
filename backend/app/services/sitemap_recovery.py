from __future__ import annotations

import asyncio
import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.sitemap_ai import review_sitemap_opportunities_ai
from app.services.sitemap_analysis import build_sitemap_analysis
from app.services.sitemap_classifier import classify_page
from app.services.sitemap_fallback import select_browser_fallback_candidates
from app.services.sitemap_fetcher import normalize_url
from app.services.sitemap_duplicate_audit import rebuild_persisted_collision_audit
from app.services.sitemap_page_resolver import (
    render_pages_browser,
    resolve_pages_http,
    resolve_pages_impersonated,
)
from app.services.sitemap_topics import preliminary_topic
from app.services.sitemap_result_views import compact_analysis_payload, result_quality

settings = get_settings()

_HARD_ACCESS = {401, 403, 406}
_TRANSIENT = {408, 425, 429, 500, 502, 503, 504}
_STALE = {404, 410}
_TAXONOMY_CONTENT_TYPES = {"PRODUCT", "CONTENT", "CATEGORY", "TOOL_CALCULATOR", "FAQ_HELP", "LANDING_PAGE"}


class SitemapRecoveryCancelled(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_sources(db: Session, run: models.SitemapRun):
    snapshot = dict((run.config_json or {}).get("_input_snapshot") or {})
    rows = list(snapshot.get("sources") or [])
    if rows:
        return [SimpleNamespace(**x) for x in rows if x.get("id") is not None]
    return (
        db.query(models.SitemapSource)
        .filter(models.SitemapSource.project_id == run.project_id)
        .order_by(models.SitemapSource.id)
        .all()
    )


def _cancel_requested(run_id: int) -> bool:
    control_db = SessionLocal()
    try:
        row = control_db.get(models.SitemapRun, run_id)
        if not row:
            return True
        if str(row.status or "").lower() in {"cancelling", "cancelled"}:
            return True
        return bool(((row.config_json or {}).get("_control") or {}).get("cancel_requested"))
    finally:
        control_db.close()


def _progress(
    db: Session,
    run: models.SitemapRun,
    *,
    phase: str,
    percent: float,
    current: int,
    total: int,
    message: str,
) -> None:
    if _cancel_requested(run.id):
        raise SitemapRecoveryCancelled()
    now = _now()
    cfg = dict(run.config_json or {})
    started = run.started_at or now
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    cfg["_progress"] = {
        "percent": round(max(0.0, min(100.0, percent)), 1),
        "overall_percent": round(max(0.0, min(100.0, percent)), 1),
        "stage_percent": round((current / max(total, 1)) * 100, 2) if total else 100.0,
        "stage_key": phase,
        "stage_label": phase.replace("_", " ").title(),
        "message": message,
        "stage_current": int(current),
        "stage_total": int(total),
        "elapsed_seconds": int(max(0.0, (now - started).total_seconds())),
        "heartbeat_at": now.isoformat(),
        "eta_seconds": None,
        "stage_eta_seconds": None,
    }
    run.config_json = cfg
    run.phase = phase
    run.progress_total = 1000
    run.progress_current = int(round(percent * 10))
    db.commit()


def _cache_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def _cache_put(db: Session, url: str, payload: dict) -> None:
    key = _cache_hash(url)
    now = _now()
    row = db.query(models.SitemapPageCache).filter_by(url_hash=key).first()
    if not row:
        row = models.SitemapPageCache(
            url_hash=key,
            normalized_url=normalize_url(url),
            payload_json={},
        )
        db.add(row)
    cache_payload = dict(payload or {})
    cache_payload["quality_version"] = 2
    row.normalized_url = normalize_url(url)
    row.payload_json = cache_payload
    row.content_hash = payload.get("content_hash")
    row.stored_at = now
    row.expires_at = now + timedelta(hours=settings.sitemap_cache_ttl_hours)


def _row_to_item(row: models.SitemapUrl) -> dict:
    evidence = row.classification_evidence_json or {}
    return {
        "requested_url": row.original_url,
        "final_url": row.final_url or row.original_url,
        "status_code": row.http_status,
        "error": row.error or "Persisted page-resolution failure",
        "retry_count": int(row.retry_count or 0),
        "terminal_state": evidence.get("terminal_state") or "fetch_failed",
        "retryable": evidence.get("retryable", True),
        "challenge_detected": evidence.get("challenge_detected", False),
    }


def _is_recoverable(row: models.SitemapUrl) -> bool:
    evidence = row.classification_evidence_json or {}
    terminal = str(evidence.get("terminal_state") or "").lower()
    if terminal == "pending_resolution" or row.error == "Pending page resolution":
        return False
    return bool(
        row.error
        and (
            row.http_status in _HARD_ACCESS
            or row.http_status in _TRANSIENT
            or row.http_status is None
            or terminal == "security_challenge"
        )
    )


def _apply_payload(row: models.SitemapUrl, payload: dict) -> bool:
    """Apply one resolver result. Returns True when the page is now usable."""
    row.final_url = payload.get("final_url") or row.final_url or row.original_url
    row.http_status = payload.get("status_code")
    row.fetch_method = payload.get("fetch_method") or row.fetch_method
    row.retry_count = int(payload.get("retry_count") or row.retry_count or 0)
    row.fetched_at = _now()

    prior_evidence = dict(row.classification_evidence_json or {})
    recovery_attempt_count = max(0, int(prior_evidence.get("recovery_attempt_count") or 0)) + 1

    if payload.get("error"):
        row.error = str(payload.get("error"))
        evidence = prior_evidence
        evidence.update({
            "terminal_state": payload.get("terminal_state") or evidence.get("terminal_state") or "fetch_failed",
            "retryable": payload.get("retryable"),
            "challenge_detected": payload.get("challenge_detected", False),
            "challenge_reason": payload.get("challenge_reason"),
            "recovery_attempted": True,
            "recovery_attempt_count": recovery_attempt_count,
            "last_recovery_at": _now().isoformat(),
            "recovery_fetch_method": payload.get("fetch_method"),
            "retry_after_seconds": payload.get("retry_after_seconds"),
        })
        row.classification_evidence_json = evidence
        return False

    classification = classify_page(payload)
    topic = preliminary_topic(payload)
    row.canonical_url = payload.get("canonical_url") or None
    row.broad_page_type = classification.get("broad_page_type", "OTHER")
    row.detailed_page_type = classification.get("detailed_page_type", "OTHER")
    row.type_confidence = float(classification.get("confidence") or 0)
    row.classification_method = classification.get("method", "deterministic")
    page_type = str(classification.get("detailed_page_type") or "OTHER").upper()
    taxonomy_eligible = page_type in _TAXONOMY_CONTENT_TYPES
    row.classification_evidence_json = {
        **classification,
        "terminal_state": payload.get("terminal_state") or "usable",
        "retryable": False,
        "challenge_detected": payload.get("challenge_detected", False),
        "challenge_reason": payload.get("challenge_reason"),
        "recovery_attempted": True,
        "recovery_attempt_count": recovery_attempt_count,
        "last_recovery_at": _now().isoformat(),
        "recovered_by_impersonation": payload.get("recovered_by_impersonation", False),
        "recovered_by_browser": payload.get("recovered_by_browser", False),
        "recovered_by_retry": payload.get("recovered_by_retry", False),
        "taxonomy_eligible": taxonomy_eligible,
        "taxonomy_exclusion_reason": None if taxonomy_eligible else f"excluded_page_type:{page_type}",
    }
    row.category = topic.get("category", "Other / Unclassified")
    row.topic = topic.get("topic", "Other / Unclassified")
    row.subtopic = topic.get("subtopic") or None
    row.topic_confidence = float(topic.get("confidence") or 0)
    row.topic_method = topic.get("method", "deterministic")
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
    row.error = None
    return True


def _refresh_source_discovery(source_discovery: list[dict], rows: list[models.SitemapUrl]) -> list[dict]:
    by_source: dict[int, Counter] = defaultdict(Counter)
    for row in rows:
        c = by_source[int(row.source_id)]
        c["discovered_urls"] += 1
        if not row.error:
            c["usable_urls"] += 1
        elif row.http_status in _STALE:
            c["stale_urls"] += 1
        elif row.http_status == 429:
            c["rate_limited_urls"] += 1
            c["failed_urls"] += 1
        elif row.classification_evidence_json and (row.classification_evidence_json or {}).get("terminal_state") == "security_challenge":
            c["security_challenge_urls"] += 1
        elif "Non-HTML" in str(row.error or ""):
            c["non_html_urls"] += 1
        else:
            c["failed_urls"] += 1

    output = []
    seen = set()
    for item in source_discovery:
        row = dict(item)
        sid = int(row.get("source_id") or 0)
        seen.add(sid)
        stats = by_source.get(sid, Counter())
        for key in (
            "usable_urls", "stale_urls", "non_html_urls", "security_challenge_urls",
            "failed_urls", "rate_limited_urls",
        ):
            row[key] = int(stats.get(key, 0))
        if row.get("status") == "failed" and row["usable_urls"] > 0:
            row["status"] = "warning"
        output.append(row)
    return output


def _store_opportunities(
    db: Session,
    run_id: int,
    opportunities: list[dict],
    evidence_index: dict[str, dict],
    *,
    enable_ai: bool,
) -> list[dict]:
    ai_reviews = {}
    ai_candidates = [x for x in opportunities if str(x.get("priority") or "").upper() in {"HIGH", "MEDIUM"}]
    if enable_ai and ai_candidates:
        ai_reviews = review_sitemap_opportunities_ai(ai_candidates, evidence_index)

    final = []
    db.execute(delete(models.SitemapOpportunity).where(models.SitemapOpportunity.sitemap_run_id == run_id))
    for opp in opportunities:
        review = ai_reviews.get(opp.get("opportunity_key"))
        if review:
            decision = str(review.get("decision") or "REVIEW").upper()
            opp["ai_decision"] = decision
            opp["ai_review_confidence"] = float(review.get("confidence") or 0)
            opp["business_relevance"] = review.get("business_relevance") or opp.get("business_relevance")
            recommended = review.get("recommended_page_type")
            if recommended and recommended != "NO_CHANGE":
                opp["recommended_page_type"] = recommended
            if review.get("recommendation"):
                opp["recommendation"] = review["recommendation"]
            opp["evidence_ids"] = list(dict.fromkeys((opp.get("evidence_ids") or []) + (review.get("evidence_ids") or [])))
            opp["source"] = "deterministic+ai-reviewed"
            if decision == "NO_CHANGE":
                continue
        final.append(opp)
        db.add(models.SitemapOpportunity(
            sitemap_run_id=run_id,
            opportunity_key=opp["opportunity_key"],
            gap_type=opp["gap_type"],
            priority=opp["priority"],
            confidence=float(opp["confidence"]),
            category=opp.get("category"),
            topic=opp.get("topic"),
            recommended_page_type=opp.get("recommended_page_type"),
            client_count=int(opp.get("client_count") or 0),
            competitor_count=int(opp.get("competitor_count") or 0),
            competitor_sites=int(opp.get("competitor_sites") or 0),
            business_relevance=opp.get("business_relevance") or "uncertain",
            title=opp["title"],
            recommendation=opp["recommendation"],
            evidence_json=opp.get("evidence_ids") or [],
            source=opp.get("source") or "deterministic",
        ))
    return final


def _recovery_attempt_count(row: models.SitemapUrl) -> int:
    evidence = row.classification_evidence_json or {}
    try:
        return max(0, int(evidence.get("recovery_attempt_count") or 0))
    except (TypeError, ValueError):
        return 0


def _clear_recovery_request(cfg: dict, *, terminal_status: str) -> dict:
    """Clear the durable recovery-request latch when recovery reaches a terminal state."""
    control = dict(cfg.get("_control") or {})
    control.update(
        {
            "recovery_requested": False,
            "recovery_completed_at": _now().isoformat(),
            "recovery_terminal_status": terminal_status,
        }
    )
    cfg["_control"] = control
    return cfg


def _recovery_batch_health(
    results: list[dict],
    *,
    min_success_ratio: float,
    block_ratio_limit: float,
) -> dict:
    """Summarise only requests that were actually launched.

    ``host_access_deferred`` rows are synthetic queue deferrals emitted by the
    resolver after a host circuit opens. They are not HTTP attempts and therefore
    must not inflate attempted counts or overwrite a persisted real HTTP status.
    """
    deferred = [
        payload
        for payload in results
        if str(payload.get("terminal_state") or "").lower() == "host_access_deferred"
    ]
    launched = [
        payload
        for payload in results
        if str(payload.get("terminal_state") or "").lower() != "host_access_deferred"
    ]
    processed = len(launched)
    successes = sum(1 for payload in launched if not payload.get("error"))
    hard_blocks = sum(
        1
        for payload in launched
        if payload.get("error") and payload.get("status_code") in _HARD_ACCESS
    )
    success_ratio = successes / max(processed, 1)
    block_ratio = hard_blocks / max(processed, 1)
    healthy = bool(
        processed > 0
        and not deferred
        and success_ratio >= min_success_ratio
        and block_ratio < block_ratio_limit
    )

    if deferred:
        reason = f"host access circuit reopened; {len(deferred):,} row(s) deferred in current batch"
    elif block_ratio >= block_ratio_limit:
        reason = f"hard-access ratio {block_ratio:.1%} reached safety threshold"
    elif success_ratio < min_success_ratio:
        reason = f"recovery success ratio {success_ratio:.1%} below {min_success_ratio:.1%}"
    else:
        reason = None

    return {
        "healthy": healthy,
        "processed": processed,
        "successes": successes,
        "hard_blocks": hard_blocks,
        "deferred": len(deferred),
        "success_ratio": success_ratio,
        "block_ratio": block_ratio,
        "reason": reason,
    }


def _recovery_cooldown_seconds(reprobe_count: int) -> float:
    initial = max(0.0, float(settings.sitemap_recovery_cooldown_initial_seconds))
    maximum = max(initial, float(settings.sitemap_recovery_cooldown_max_seconds))
    multiplier = max(1.0, float(settings.sitemap_recovery_cooldown_multiplier))
    exponent = max(0, int(reprobe_count) - 1)
    return min(maximum, initial * (multiplier**exponent))


def _next_bulk_interval_seconds(current: float, *, healthy: bool) -> float:
    minimum = max(0.0, float(settings.sitemap_recovery_bulk_min_interval_seconds))
    initial = max(minimum, float(settings.sitemap_recovery_bulk_initial_interval_seconds))
    probe_interval = max(initial, float(settings.sitemap_recovery_probe_interval_seconds))
    value = max(minimum, float(current or initial))

    if healthy:
        factor = max(0.05, min(1.0, float(settings.sitemap_recovery_rate_ramp_factor)))
        return max(minimum, value * factor)

    factor = max(1.0, float(settings.sitemap_recovery_rate_backoff_factor))
    return min(probe_interval, max(initial, value * factor))


def _healthy_bulk_rate_update(current: float, healthy_streak: int) -> tuple[float, int, bool]:
    """Return (next_interval, next_streak, accelerated).

    A single successful bulk batch is not enough evidence to speed up a host.
    The current interval becomes a learned-safe interval immediately, but the
    controller only experiments with a faster rate after N consecutive complete
    healthy bulk batches.
    """
    required = max(1, int(settings.sitemap_recovery_healthy_batches_before_ramp))
    streak = max(0, int(healthy_streak)) + 1
    if streak < required:
        return float(current), streak, False
    return _next_bulk_interval_seconds(float(current), healthy=True), 0, True


def _pressure_backoff_interval(current: float, last_known_safe: float | None) -> float:
    """Back off to at least the most recently proven sustainable rate.

    If 5s/request completed healthy batches but an experiment at 4.5s/request
    reopened the WAF circuit, the controller returns to 5s instead of immediately
    oscillating back to the failing rate.
    """
    backed_off = _next_bulk_interval_seconds(float(current), healthy=False)
    if last_known_safe is None:
        return backed_off
    try:
        safe = float(last_known_safe)
    except (TypeError, ValueError):
        return backed_off
    return max(backed_off, safe)


def _persist_recovery_state(
    db: Session,
    run: models.SitemapRun,
    recovery_state: dict,
) -> None:
    recovery_state["updated_at"] = _now().isoformat()
    cfg = dict(run.config_json or {})
    cfg["_recovery_state"] = recovery_state
    run.config_json = cfg
    db.commit()


async def _sleep_with_recovery_heartbeat(
    db: Session,
    run: models.SitemapRun,
    *,
    seconds: float,
    host: str,
    progress_percent: float,
    current: int,
    total: int,
) -> None:
    """Sleep without looking stale to the worker supervisor."""
    remaining = max(0.0, float(seconds))
    heartbeat = max(1.0, float(settings.sitemap_recovery_heartbeat_seconds))
    while remaining > 0:
        _progress(
            db,
            run,
            phase="recovery_cooldown",
            percent=progress_percent,
            current=current,
            total=total,
            message=f"Recovery cooling down {host}; retrying health probe in {int(remaining + 0.999):,}s",
        )
        chunk = min(heartbeat, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


async def recover_sitemap_run(
    run_id: int,
    *,
    db: Session | None = None,
    background: bool = False,
) -> None:
    """Recover only persisted failed URLs, then rebuild derived analysis in-place.

    No sitemap XML is rediscovered and no already-successful URL is refetched.
    Hard-access recovery is a durable host controller: slow health probe -> paced
    bulk recovery -> automatic cooldown -> slow re-probe -> resume. State is
    checkpointed in ``config_json['_recovery_state']`` so a supervised worker
    restart can continue from persisted rows without rediscovering the sitemap.
    """
    owns_db = db is None
    db = db or SessionLocal()
    try:
        run = db.get(models.SitemapRun, run_id)
        if not run:
            return

        cfg = dict(run.config_json or {})
        background_meta = dict(cfg.get("_background_recovery") or {})
        base_status = str(background_meta.get("base_status") or run.status or "completed_with_warnings")
        base_phase = str(background_meta.get("base_phase") or run.phase or "complete_with_warnings")

        if background:
            background_meta.update({
                "state": "running",
                "started_at": background_meta.get("started_at") or _now().isoformat(),
                "last_slice_started_at": _now().isoformat(),
                "next_attempt_at": None,
            })
            cfg["_background_recovery"] = background_meta
            run.config_json = cfg
            # Keep the report terminal/exportable while recovery executes.
            run.error = None if base_status != "partial" else run.error
        else:
            run.status = "running"
            run.completed_at = None
            run.error = None

        run.phase = "background_recovery_preparing" if background else "recovery_preparing"
        run.started_at = run.started_at or _now()

        recovery_options = dict(cfg.get("_recovery_only") or {})
        allow_browser = bool(recovery_options.get("browser", True))
        recovery_state = dict(cfg.get("_recovery_state") or {})
        if int(recovery_state.get("version") or 0) != 2:
            prior_hosts = dict(recovery_state.get("hosts") or {})
            learned_hosts = dict(recovery_state.get("learned_hosts") or {})
            for host, prior in prior_hosts.items():
                prior = dict(prior or {})
                learned = dict(learned_hosts.get(host) or {})
                safe = (
                    prior.get("last_known_safe_interval_seconds")
                    or prior.get("bulk_interval_seconds")
                )
                if safe is not None:
                    learned["last_known_safe_interval_seconds"] = safe
                learned_hosts[host] = learned
            recovery_state = {
                "version": 2,
                "requested_at": recovery_options.get("requested_at") or _now().isoformat(),
                "hosts": {},
                "learned_hosts": learned_hosts,
                "attempted_total": 0,
                "recovered_total": 0,
            }
        recovery_state.update(
            {
                "status": "running",
                "run_id": int(run.id),
                "started_at": recovery_state.get("started_at") or _now().isoformat(),
            }
        )
        cfg["_recovery_state"] = recovery_state
        run.config_json = cfg
        db.commit()

        rows = (
            db.query(models.SitemapUrl)
            .filter(models.SitemapUrl.sitemap_run_id == run_id)
            .order_by(models.SitemapUrl.id)
            .all()
        )
        candidates = [row for row in rows if _is_recoverable(row)]
        total = len(candidates)
        _progress(
            db,
            run,
            phase="recovery_preparing",
            percent=2,
            current=0,
            total=total,
            message=f"Preparing recovery for {total:,} persisted failed URL(s); successful URLs will not be refetched",
        )

        if not candidates:
            cfg = dict(run.config_json or {})
            cfg.pop("_recovery_only", None)
            cfg["_recovery"] = {
                "completed_at": _now().isoformat(),
                "requested": 0,
                "attempted": 0,
                "recovered": 0,
                "remaining": 0,
            }
            recovery_state.update({"status": "completed", "completed_at": _now().isoformat()})
            cfg["_recovery_state"] = recovery_state
            if background:
                bg = dict(cfg.get("_background_recovery") or {})
                bg.update({
                    "state": "completed",
                    "completed_at": _now().isoformat(),
                    "next_attempt_at": None,
                    "recoverable_rows": 0,
                    "slices_completed": int(bg.get("slices_completed") or 0) + 1,
                })
                cfg["_background_recovery"] = bg
                run.status = base_status
                run.phase = base_phase
            else:
                cfg = _clear_recovery_request(cfg, terminal_status="completed")
                run.status = "completed"
                run.phase = "complete"
                run.completed_at = _now()
            run.config_json = cfg
            db.commit()
            return

        row_by_id = {int(row.id): row for row in candidates}
        item_by_id = {
            int(row.id): {"row_id": int(row.id), **_row_to_item(row)}
            for row in candidates
        }
        recovered_ids: set[int] = set()
        attempted = int(recovery_state.get("attempted_total") or 0)
        recovered_total = int(recovery_state.get("recovered_total") or 0)
        checkpoint_every = max(10, int(settings.sitemap_checkpoint_every))
        cache_buffer: list[tuple[str, dict]] = []
        background_next_attempt_at: datetime | None = None

        async def apply_result(
            item: dict,
            payload: dict,
            phase: str,
            base_pct: float,
            span_pct: float,
            done: int,
            phase_total: int,
        ):
            nonlocal attempted, recovered_total
            if str(payload.get("terminal_state") or "").lower() == "host_access_deferred":
                return

            row_id = int(item["row_id"])
            row = row_by_id[row_id]
            attempted += 1
            if _apply_payload(row, payload):
                if row_id not in recovered_ids:
                    recovered_ids.add(row_id)
                    recovered_total += 1
                cache_buffer.append((row.original_url, payload))

            recovery_state["attempted_total"] = attempted
            recovery_state["recovered_total"] = recovered_total

            if len(cache_buffer) >= checkpoint_every:
                for url, p in cache_buffer:
                    _cache_put(db, url, p)
                cache_buffer.clear()
                db.commit()

            if done == phase_total or done % max(10, int(settings.sitemap_progress_update_every)) == 0:
                _progress(
                    db,
                    run,
                    phase=phase,
                    percent=base_pct + span_pct * (done / max(phase_total, 1)),
                    current=done,
                    total=phase_total,
                    message=(
                        f"Recovery {phase.replace('_', ' ')} ({done:,}/{phase_total:,}); "
                        f"recovered {recovered_total:,}"
                    ),
                )

        # ------------------------------------------------------------------
        # 1) Hard 401/403/406/challenge/host-deferred failures.
        # ------------------------------------------------------------------
        hard_items: list[dict] = []
        for row in candidates:
            evidence = row.classification_evidence_json or {}
            terminal = str(evidence.get("terminal_state") or "").lower()
            if row.http_status in _HARD_ACCESS or terminal in {"security_challenge", "host_access_deferred"}:
                hard_items.append(item_by_id[int(row.id)])

        recovery_host_outcomes: dict[str, dict] = {}
        if hard_items:
            hard_by_host: dict[str, list[dict]] = defaultdict(list)
            for item in hard_items:
                url = str(item.get("final_url") or item.get("requested_url") or "")
                host = (urlparse(url).hostname or "").lower() or "unknown"
                hard_by_host[host].append(item)

            # Small hosts complete first so a 180k-URL host never starves all others.
            ordered_hosts = sorted(hard_by_host.items(), key=lambda pair: (len(pair[1]), pair[0]))

            hard_seen_ids: set[int] = set()
            probe_size = max(1, int(settings.sitemap_recovery_hard_probe_batch_size))
            base_batch_size = max(probe_size, int(settings.sitemap_recovery_hard_batch_size))
            max_batch_size = max(base_batch_size, int(settings.sitemap_recovery_hard_max_batch_size))
            hard_concurrency = max(1, int(settings.sitemap_recovery_hard_concurrency))
            recovery_per_host = max(1, int(settings.sitemap_recovery_bulk_per_host_concurrency))
            min_success_ratio = max(0.0, min(1.0, float(settings.sitemap_recovery_hard_min_success_ratio)))
            block_ratio_limit = max(0.0, min(1.0, float(settings.sitemap_recovery_hard_block_ratio)))
            batch_pause_seconds = max(0.0, float(settings.sitemap_recovery_hard_batch_pause_seconds))
            probe_interval = max(0.0, float(settings.sitemap_recovery_probe_interval_seconds))
            min_bulk_interval = max(0.0, float(settings.sitemap_recovery_bulk_min_interval_seconds))
            initial_bulk_interval = max(min_bulk_interval, float(settings.sitemap_recovery_bulk_initial_interval_seconds))
            max_consecutive_pressure = max(
                0, int(settings.sitemap_recovery_max_consecutive_pressure_events)
            )
            max_cycles = max(1, int(settings.sitemap_recovery_max_cycles_per_host))
            max_attempts_per_url = max(1, int(settings.sitemap_recovery_max_attempts_per_url))

            state_hosts = dict(recovery_state.get("hosts") or {})
            learned_hosts = dict(recovery_state.get("learned_hosts") or {})

            for host, host_items in ordered_hosts:
                previous = dict(state_hosts.get(host) or {})
                learned = dict(learned_hosts.get(host) or {})
                reprobe_count = max(0, int(previous.get("reprobe_count") or 0))
                consecutive_pressure = max(
                    0, int(previous.get("consecutive_pressure_events") or 0)
                )
                healthy_bulk_streak = max(
                    0, int(previous.get("healthy_bulk_streak") or 0)
                )
                cycles_completed = max(0, int(previous.get("cycles_completed") or 0))
                last_known_safe_interval = (
                    previous.get("last_known_safe_interval_seconds")
                    or learned.get("last_known_safe_interval_seconds")
                )
                starting_interval = (
                    last_known_safe_interval
                    or previous.get("bulk_interval_seconds")
                    or initial_bulk_interval
                )
                bulk_interval = float(starting_interval)
                bulk_interval = min(
                    probe_interval or bulk_interval,
                    max(min_bulk_interval, bulk_interval),
                )
                host_processed = max(0, int(previous.get("processed_attempts") or 0))
                host_recovered = max(0, int(previous.get("recovered") or 0))
                host_paused = bool(
                    previous.get("state") == "paused"
                    and consecutive_pressure > max_consecutive_pressure
                )
                host_deferred = False
                pause_reason = previous.get("pause_reason") if host_paused else None
                last_success_ratio = float(previous.get("last_success_ratio") or 0.0)
                last_block_ratio = float(previous.get("last_block_ratio") or 0.0)

                # If the worker was restarted during a persisted cooldown, honour the
                # remaining delay instead of immediately re-hitting the host.
                raw_cooldown_until = previous.get("cooldown_until")
                if raw_cooldown_until and not host_paused:
                    try:
                        cooldown_until = datetime.fromisoformat(str(raw_cooldown_until))
                        if cooldown_until.tzinfo is None:
                            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
                        remaining_cooldown = max(0.0, (cooldown_until - _now()).total_seconds())
                    except Exception:
                        remaining_cooldown = 0.0
                    if remaining_cooldown > 0:
                        if background:
                            host_deferred = True
                            background_next_attempt_at = min(
                                [x for x in (background_next_attempt_at, cooldown_until) if x is not None]
                            ) if background_next_attempt_at else cooldown_until
                        else:
                            await _sleep_with_recovery_heartbeat(
                                db,
                                run,
                                seconds=remaining_cooldown,
                                host=host,
                                progress_percent=5.0,
                                current=min(len(hard_seen_ids), len(hard_items)),
                                total=len(hard_items),
                            )

                # Least-attempted rows are always tried first. This makes a supervised
                # process restart naturally continue through untouched rows rather than
                # repeatedly hammering the same prefix.
                host_items.sort(
                    key=lambda item: (
                        _recovery_attempt_count(row_by_id[int(item["row_id"])]),
                        int(item["row_id"]),
                    )
                )

                while cycles_completed < max_cycles and not host_paused and not host_deferred:
                    pending = [
                        item
                        for item in host_items
                        if row_by_id[int(item["row_id"])].error
                        and _is_recoverable(row_by_id[int(item["row_id"])])
                        and _recovery_attempt_count(row_by_id[int(item["row_id"])]) < max_attempts_per_url
                    ]
                    pending.sort(
                        key=lambda item: (
                            _recovery_attempt_count(row_by_id[int(item["row_id"])]),
                            int(item["row_id"]),
                        )
                    )
                    if not pending:
                        break

                    cursor = 0
                    batch_size = probe_size
                    probe_mode = True

                    while cursor < len(pending):
                        batch = pending[cursor : cursor + batch_size]
                        interval = probe_interval if probe_mode else bulk_interval
                        call_concurrency = 1 if probe_mode else hard_concurrency
                        call_per_host = 1 if probe_mode else recovery_per_host

                        async def hard_cb(index: int, payload: dict):
                            terminal = str(payload.get("terminal_state") or "").lower()
                            if terminal == "host_access_deferred":
                                return
                            item = batch[index]
                            hard_seen_ids.add(int(item["row_id"]))
                            hard_done = min(len(hard_seen_ids), len(hard_items))
                            await apply_result(
                                item,
                                payload,
                                "recovery_impersonated_http",
                                5,
                                25,
                                hard_done,
                                len(hard_items),
                            )

                        results = await resolve_pages_impersonated(
                            batch,
                            result_callback=hard_cb,
                            concurrency=call_concurrency,
                            per_host_limit=call_per_host,
                            initial_per_host_limit=1,
                            min_start_interval_seconds=interval,
                        )

                        health = _recovery_batch_health(
                            results,
                            min_success_ratio=min_success_ratio,
                            block_ratio_limit=block_ratio_limit,
                        )
                        launched_now = int(health["processed"])
                        success_now = int(health["successes"])
                        host_processed += launched_now
                        host_recovered += success_now
                        last_success_ratio = float(health["success_ratio"])
                        last_block_ratio = float(health["block_ratio"])

                        # The resolver emits deferred rows only after the host circuit
                        # opens. They are the unlaunched tail of this same batch, so
                        # advance only by real HTTP attempts and retry that tail after
                        # cooldown/re-probe.
                        cursor += launched_now

                        host_state = {
                            "state": "probing" if probe_mode else "recovering",
                            "candidate_rows": len(host_items),
                            "processed_attempts": host_processed,
                            "recovered": host_recovered,
                            "cycles_completed": cycles_completed,
                            "reprobe_count": reprobe_count,
                            "consecutive_pressure_events": consecutive_pressure,
                            "healthy_bulk_streak": healthy_bulk_streak,
                            "last_known_safe_interval_seconds": (
                                round(float(last_known_safe_interval), 4)
                                if last_known_safe_interval is not None
                                else None
                            ),
                            "batch_size": batch_size,
                            "bulk_interval_seconds": round(bulk_interval, 4),
                            "active_interval_seconds": round(interval, 4),
                            "last_success_ratio": round(last_success_ratio, 4),
                            "last_block_ratio": round(last_block_ratio, 4),
                            "last_deferred": int(health["deferred"]),
                            "pause_reason": None,
                            "updated_at": _now().isoformat(),
                        }
                        state_hosts[host] = host_state
                        recovery_state["hosts"] = state_hosts
                        recovery_state["attempted_total"] = attempted
                        recovery_state["recovered_total"] = recovered_total
                        _persist_recovery_state(db, run, recovery_state)

                        if health["healthy"]:
                            if probe_mode:
                                # A clean probe proves reachability, not sustained bulk
                                # capacity. Do not reset pressure history until a full
                                # bulk batch also succeeds.
                                probe_mode = False
                                batch_size = base_batch_size
                                host_state["state"] = "recovering"
                            else:
                                # A complete healthy bulk batch is meaningful progress.
                                # Reset the consecutive pressure/cooldown ladder, learn
                                # this interval as safe, and only accelerate after several
                                # consecutive healthy batches at the current rate.
                                consecutive_pressure = 0
                                last_known_safe_interval = float(interval)
                                learned = dict(learned_hosts.get(host) or {})
                                learned["last_known_safe_interval_seconds"] = round(
                                    last_known_safe_interval, 4
                                )
                                learned["updated_at"] = _now().isoformat()
                                learned_hosts[host] = learned
                                recovery_state["learned_hosts"] = learned_hosts

                                bulk_interval, healthy_bulk_streak, _ = _healthy_bulk_rate_update(
                                    bulk_interval, healthy_bulk_streak
                                )
                                batch_size = min(
                                    max_batch_size,
                                    max(base_batch_size, batch_size * 2),
                                )

                            host_state["consecutive_pressure_events"] = consecutive_pressure
                            host_state["healthy_bulk_streak"] = healthy_bulk_streak
                            host_state["last_known_safe_interval_seconds"] = (
                                round(float(last_known_safe_interval), 4)
                                if last_known_safe_interval is not None
                                else None
                            )
                            host_state["bulk_interval_seconds"] = round(bulk_interval, 4)
                            host_state["batch_size"] = batch_size
                            host_state["updated_at"] = _now().isoformat()
                            state_hosts[host] = host_state
                            recovery_state["hosts"] = state_hosts
                            _persist_recovery_state(db, run, recovery_state)

                            if cursor < len(pending) and batch_pause_seconds:
                                await asyncio.sleep(batch_pause_seconds)
                            continue

                        # Access pressure returned. Do not end the host permanently:
                        # back off request rate, cool down, then run another slow probe.
                        reprobe_count += 1
                        consecutive_pressure += 1
                        healthy_bulk_streak = 0
                        bulk_interval = _pressure_backoff_interval(
                            bulk_interval, last_known_safe_interval
                        )
                        pause_reason = str(health.get("reason") or "host health probe failed")

                        if consecutive_pressure > max_consecutive_pressure:
                            host_paused = True
                            pause_reason = (
                                f"{pause_reason}; maximum {max_consecutive_pressure} consecutive "
                                "pressure/no-progress event(s) exhausted"
                            )
                            host_state.update(
                                {
                                    "state": "paused",
                                    "reprobe_count": reprobe_count,
                                    "consecutive_pressure_events": consecutive_pressure,
                                    "healthy_bulk_streak": healthy_bulk_streak,
                                    "last_known_safe_interval_seconds": (
                                        round(float(last_known_safe_interval), 4)
                                        if last_known_safe_interval is not None
                                        else None
                                    ),
                                    "pause_reason": pause_reason,
                                    "bulk_interval_seconds": round(bulk_interval, 4),
                                    "updated_at": _now().isoformat(),
                                }
                            )
                            state_hosts[host] = host_state
                            recovery_state["hosts"] = state_hosts
                            _persist_recovery_state(db, run, recovery_state)
                            break

                        cooldown = _recovery_cooldown_seconds(consecutive_pressure)
                        cooldown_until = _now() + timedelta(seconds=cooldown)
                        host_state.update(
                            {
                                "state": "cooling",
                                "reprobe_count": reprobe_count,
                                "consecutive_pressure_events": consecutive_pressure,
                                "healthy_bulk_streak": healthy_bulk_streak,
                                "last_known_safe_interval_seconds": (
                                    round(float(last_known_safe_interval), 4)
                                    if last_known_safe_interval is not None
                                    else None
                                ),
                                "pause_reason": pause_reason,
                                "cooldown_seconds": cooldown,
                                "cooldown_until": cooldown_until.isoformat(),
                                "bulk_interval_seconds": round(bulk_interval, 4),
                                "updated_at": _now().isoformat(),
                            }
                        )
                        state_hosts[host] = host_state
                        recovery_state["hosts"] = state_hosts
                        _persist_recovery_state(db, run, recovery_state)

                        hard_done = min(len(hard_seen_ids), len(hard_items))
                        hard_pct = 5 + 25 * (hard_done / max(len(hard_items), 1))
                        if background:
                            host_deferred = True
                            if background_next_attempt_at is None or cooldown_until < background_next_attempt_at:
                                background_next_attempt_at = cooldown_until
                            break

                        await _sleep_with_recovery_heartbeat(
                            db,
                            run,
                            seconds=cooldown,
                            host=host,
                            progress_percent=hard_pct,
                            current=hard_done,
                            total=len(hard_items),
                        )

                        probe_mode = True
                        batch_size = probe_size
                        host_state.update(
                            {
                                "state": "probing",
                                "cooldown_until": None,
                                "updated_at": _now().isoformat(),
                            }
                        )
                        state_hosts[host] = host_state
                        recovery_state["hosts"] = state_hosts
                        _persist_recovery_state(db, run, recovery_state)

                    if host_paused or host_deferred:
                        break

                    cycles_completed += 1
                    host_state = dict(state_hosts.get(host) or {})
                    host_state["cycles_completed"] = cycles_completed
                    host_state["updated_at"] = _now().isoformat()
                    state_hosts[host] = host_state
                    recovery_state["hosts"] = state_hosts
                    _persist_recovery_state(db, run, recovery_state)

                remaining_recoverable = sum(
                    1
                    for item in host_items
                    if row_by_id[int(item["row_id"])].error
                    and _is_recoverable(row_by_id[int(item["row_id"])])
                )
                attempt_exhausted = sum(
                    1
                    for item in host_items
                    if row_by_id[int(item["row_id"])].error
                    and _recovery_attempt_count(row_by_id[int(item["row_id"])]) >= max_attempts_per_url
                )
                never_attempted = sum(
                    1
                    for item in host_items
                    if _recovery_attempt_count(row_by_id[int(item["row_id"])]) == 0
                )

                final_state = (
                    "cooling" if host_deferred
                    else "paused" if host_paused
                    else "complete" if remaining_recoverable == 0
                    else "bounded_complete"
                )
                final_host_state = dict(state_hosts.get(host) or {})
                final_host_state.update(
                    {
                        "state": final_state,
                        "candidate_rows": len(host_items),
                        "processed_attempts": host_processed,
                        "recovered": host_recovered,
                        "cycles_completed": cycles_completed,
                        "reprobe_count": reprobe_count,
                        "consecutive_pressure_events": consecutive_pressure,
                        "healthy_bulk_streak": healthy_bulk_streak,
                        "last_known_safe_interval_seconds": (
                            round(float(last_known_safe_interval), 4)
                            if last_known_safe_interval is not None
                            else None
                        ),
                        "remaining_recoverable": remaining_recoverable,
                        "attempt_exhausted": attempt_exhausted,
                        "never_attempted": never_attempted,
                        "bulk_interval_seconds": round(bulk_interval, 4),
                        "last_success_ratio": round(last_success_ratio, 4),
                        "last_block_ratio": round(last_block_ratio, 4),
                        "pause_reason": pause_reason if host_paused else None,
                        "updated_at": _now().isoformat(),
                    }
                )
                state_hosts[host] = final_host_state
                recovery_state["hosts"] = state_hosts
                _persist_recovery_state(db, run, recovery_state)

                recovery_host_outcomes[host] = {
                    "candidate_rows": len(host_items),
                    "processed_this_pass": host_processed,
                    "recovered_this_pass": host_recovered,
                    "remaining_unattempted_this_pass": never_attempted,
                    "remaining_recoverable_after_pass": remaining_recoverable,
                    "attempt_exhausted": attempt_exhausted,
                    "cycles_completed": cycles_completed,
                    "reprobe_count": reprobe_count,
                    "consecutive_pressure_events": consecutive_pressure,
                    "healthy_bulk_streak": healthy_bulk_streak,
                    "last_known_safe_interval_seconds": (
                        round(float(last_known_safe_interval), 4)
                        if last_known_safe_interval is not None
                        else None
                    ),
                    "final_bulk_interval_seconds": round(bulk_interval, 4),
                    "last_success_ratio": round(last_success_ratio, 4),
                    "last_block_ratio": round(last_block_ratio, 4),
                    "paused_for_access_pressure": host_paused,
                    "cooling_for_access_pressure": host_deferred,
                    "cooldown_until": final_host_state.get("cooldown_until"),
                    "pause_reason": pause_reason if (host_paused or host_deferred) else None,
                }

        # ------------------------------------------------------------------
        # 2) Transient/rate-limited failures: adaptive host-fair HTTP recovery.
        # ------------------------------------------------------------------
        transient_items = []
        for row in candidates:
            if int(row.id) in recovered_ids:
                continue
            evidence = row.classification_evidence_json or {}
            terminal = str(evidence.get("terminal_state") or "").lower()
            if terminal in {"security_challenge", "host_access_deferred"}:
                continue
            if row.http_status in _TRANSIENT or row.http_status is None:
                transient_items.append(item_by_id[int(row.id)])

        if transient_items:
            urls = [x.get("final_url") or x.get("requested_url") for x in transient_items]

            async def transient_cb(index: int, payload: dict):
                if not payload.get("error"):
                    payload["recovered_by_retry"] = True
                await apply_result(
                    transient_items[index],
                    payload,
                    "recovery_adaptive_http",
                    30,
                    35,
                    index + 1,
                    len(transient_items),
                )

            await resolve_pages_http(
                urls,
                result_callback=transient_cb,
                concurrency=settings.sitemap_resolve_concurrency,
            )

        # ------------------------------------------------------------------
        # 3) Remaining hard blocks only: bounded Chromium last resort.
        # ------------------------------------------------------------------
        paused_hard_hosts = {
            host
            for host, outcome in recovery_host_outcomes.items()
            if outcome.get("paused_for_access_pressure") or outcome.get("cooling_for_access_pressure")
        }
        browser_raw = []
        for row in candidates:
            if int(row.id) in recovered_ids:
                continue
            row_url = str(row.final_url or row.original_url or "")
            row_host = (urlparse(row_url).hostname or "").lower() or "unknown"
            if row_host in paused_hard_hosts:
                continue
            evidence = row.classification_evidence_json or {}
            terminal = str(evidence.get("terminal_state") or "").lower()
            if row.http_status in _HARD_ACCESS or terminal in {"security_challenge", "host_access_deferred"}:
                browser_raw.append({"row_id": int(row.id), **_row_to_item(row)})

        browser_items, _ = select_browser_fallback_candidates(
            browser_raw,
            max_pages=settings.sitemap_browser_fallback_max_pages,
            per_host_cap=settings.sitemap_browser_fallback_per_host_cap,
            per_family_cap=settings.sitemap_browser_family_sample_size,
            stale_cap=0,
        )
        if browser_items and allow_browser:

            async def browser_cb(index: int, payload: dict):
                await apply_result(
                    browser_items[index],
                    payload,
                    "recovery_browser_fallback",
                    65,
                    15,
                    index + 1,
                    len(browser_items),
                )

            await render_pages_browser(
                browser_items,
                result_callback=browser_cb,
                concurrency=settings.sitemap_playwright_concurrency,
            )

        if cache_buffer:
            for url, payload in cache_buffer:
                _cache_put(db, url, payload)
            cache_buffer.clear()
            db.commit()

        recovery_state["status"] = "rebuilding_analysis"
        recovery_state["attempted_total"] = attempted
        recovery_state["recovered_total"] = recovered_total
        _persist_recovery_state(db, run, recovery_state)

        _progress(
            db,
            run,
            phase="recovery_rebuild_analysis",
            percent=82,
            current=0,
            total=1,
            message="Rebuilding comparison/opportunity outputs from persisted rows; no sitemap rediscovery",
        )

        persisted = (
            db.query(models.SitemapUrl)
            .filter(models.SitemapUrl.sitemap_run_id == run_id)
            .order_by(models.SitemapUrl.id)
            .all()
        )
        rebuilt_collisions = rebuild_persisted_collision_audit(
            db,
            run_id,
            persisted,
            audit_stage="recovery_refresh",
        )
        db.commit()
        cfg = dict(run.config_json or {})
        source_discovery = list(
            cfg.get("_sitemap_discovery")
            or (run.analysis_json or {}).get("source_discovery")
            or []
        )
        source_discovery = _refresh_source_discovery(source_discovery, persisted)
        cfg["_sitemap_discovery"] = source_discovery
        sources = _snapshot_sources(db, run)
        analysis, opportunities, evidence_index = build_sitemap_analysis(
            persisted,
            sources,
            source_discovery=source_discovery,
        )
        old_analysis = dict(run.analysis_json or {})
        if old_analysis.get("dynamic_topic_clusters_total") is not None:
            analysis["dynamic_topic_clusters_total"] = int(
                old_analysis.get("dynamic_topic_clusters_total") or 0
            )
        if old_analysis.get("sitemap_failures"):
            analysis["sitemap_failures"] = old_analysis.get("sitemap_failures")

        final_opportunities = _store_opportunities(
            db,
            run_id,
            opportunities,
            evidence_index,
            enable_ai=(
                bool(cfg.get("enable_ai", True))
                and settings.ai_mode != "off"
                and bool((analysis.get("comparison_status") or {}).get("ready"))
            ),
        )
        analysis["content_opportunities"] = final_opportunities

        remaining_rows = [row for row in persisted if row.error]
        remaining_recoverable = sum(1 for row in remaining_rows if _is_recoverable(row))
        status_counts = Counter(str(row.http_status) for row in remaining_rows)
        recovery_summary = {
            "requested": total,
            "attempted": attempted,
            "recovered": recovered_total,
            "remaining_failures": len(remaining_rows),
            "remaining_recoverable": remaining_recoverable,
            "remaining_status_counts": dict(status_counts),
            "host_outcomes": recovery_host_outcomes,
            "rebuilt_duplicate_collision_rows": int(rebuilt_collisions),
            "controller": "adaptive-learned-rate-v2",
            "completed_at": _now().isoformat(),
        }
        analysis["recovery_summary"] = recovery_summary
        quality = result_quality(db, run_id, opportunity_count=len(final_opportunities))
        run.analysis_json = compact_analysis_payload(
            analysis,
            quality=quality,
            opportunity_count=len(final_opportunities),
        )

        cfg = dict(run.config_json or {})
        cfg.pop("_recovery_only", None)
        cfg["_recovery"] = recovery_summary
        duplicate_audit = dict(cfg.get("_duplicate_audit") or {})
        duplicate_audit.update({
            "version": 1,
            "recovery_refreshed_at": recovery_summary["completed_at"],
            "recovery_collision_rows": int(rebuilt_collisions),
        })
        cfg["_duplicate_audit"] = duplicate_audit

        comparison_ready = bool((analysis.get("comparison_status") or {}).get("ready"))
        if not comparison_ready:
            run.status = "partial"
            run.phase = "partial"
            run.error = (analysis.get("comparison_status") or {}).get("message")
        elif remaining_rows:
            run.status = "completed_with_warnings"
            run.phase = "complete_with_warnings"
            run.error = None
        else:
            run.status = "completed"
            run.phase = "complete"
            run.error = None

        recovery_state.update(
            {
                "status": run.phase,
                "attempted_total": attempted,
                "recovered_total": recovered_total,
                "remaining_recoverable": remaining_recoverable,
                "completed_at": _now().isoformat(),
            }
        )
        cfg["_recovery_state"] = recovery_state

        if background:
            bg = dict(cfg.get("_background_recovery") or {})
            slices = int(bg.get("slices_completed") or 0) + 1
            if remaining_recoverable <= 0:
                bg_state = "completed"
                next_attempt = None
            elif background_next_attempt_at is not None:
                bg_state = "cooling"
                next_attempt = background_next_attempt_at.isoformat()
            elif any(x.get("paused_for_access_pressure") for x in recovery_host_outcomes.values()):
                bg_state = "bounded_complete"
                next_attempt = None
            else:
                bg_state = "queued"
                next_attempt = (
                    _now() + timedelta(
                        seconds=max(1.0, float(settings.sitemap_background_recovery_retry_delay_seconds))
                    )
                ).isoformat()
            bg.update({
                "state": bg_state,
                "next_attempt_at": next_attempt,
                "last_slice_completed_at": _now().isoformat(),
                "slices_completed": slices,
                "recoverable_rows": int(remaining_recoverable),
                "last_recovered_total": int(recovered_total),
                "base_status": run.status,
                "base_phase": run.phase,
            })
            if bg_state in {"completed", "bounded_complete"}:
                bg["completed_at"] = _now().isoformat()
            cfg["_background_recovery"] = bg
            cfg.pop("_recovery_only", None)
        else:
            cfg = _clear_recovery_request(cfg, terminal_status=run.status)
            run.completed_at = _now()

        run.config_json = cfg
        db.commit()

        if not background:
            _progress(
                db,
                run,
                phase=run.phase,
                percent=100,
                current=1,
                total=1,
                message=(
                    f"Recovery complete: {recovered_total:,}/{total:,} recovered; "
                    f"{len(remaining_rows):,} failure/warning row(s) remain"
                ),
            )

    except SitemapRecoveryCancelled:
        db.rollback()
        run = db.get(models.SitemapRun, run_id)
        if run:
            cfg = dict(run.config_json or {})
            cfg.pop("_recovery_only", None)
            state = dict(cfg.get("_recovery_state") or {})
            state.update({"status": "cancelled", "completed_at": _now().isoformat()})
            cfg["_recovery_state"] = state
            if background:
                bg = dict(cfg.get("_background_recovery") or {})
                bg.update({"state": "cancelled", "completed_at": _now().isoformat(), "next_attempt_at": None})
                cfg["_background_recovery"] = bg
            else:
                cfg = _clear_recovery_request(cfg, terminal_status="cancelled")
                run.status = "cancelled"
                run.phase = "cancelled"
                run.completed_at = _now()
            run.config_json = cfg
            run.error = None
            db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(models.SitemapRun, run_id)
        if run:
            cfg = dict(run.config_json or {})
            cfg.pop("_recovery_only", None)
            state = dict(cfg.get("_recovery_state") or {})
            state.update(
                {
                    "status": "failed",
                    "completed_at": _now().isoformat(),
                    "error": str(exc),
                }
            )
            cfg["_recovery_state"] = state
            if background:
                bg = dict(cfg.get("_background_recovery") or {})
                bg.update({
                    "state": "queued",
                    "last_error": str(exc),
                    "next_attempt_at": (
                        _now() + timedelta(
                            seconds=max(1.0, float(settings.sitemap_background_recovery_retry_delay_seconds))
                        )
                    ).isoformat(),
                })
                cfg["_background_recovery"] = bg
                run.status = str(bg.get("base_status") or run.status or "completed_with_warnings")
                run.phase = str(bg.get("base_phase") or run.phase or "complete_with_warnings")
                run.error = None if run.status != "partial" else run.error
            else:
                cfg = _clear_recovery_request(cfg, terminal_status="failed")
                run.status = "failed"
                run.phase = "recovery_failed"
                run.error = f"Recovery failed: {exc}"
                run.completed_at = _now()
            run.config_json = cfg
            db.commit()
        raise
    finally:
        if owns_db:
            db.close()


