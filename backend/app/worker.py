from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing as mp
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.analysis_contract import sanitize_run_payload

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")

RunKind = Literal["page", "sitemap", "sitemap_recovery"]

_TERMINAL_PAGE = {"completed", "failed", "dispatch_failed", "cancelled"}
_TERMINAL_SITEMAP = {
    "completed",
    "completed_with_warnings",
    "partial",
    "failed",
    "dispatch_failed",
    "cancelled",
}

# Rows that may legitimately be recovered/claimed.
_LIVE = {"queued", "claimed", "running", "cancelling"}

# ---------------------------------------------------------------------------
# Supervisor configuration
# ---------------------------------------------------------------------------
#
# Important design rule:
# A missing DB progress heartbeat is NOT by itself proof that a large HTTP
# resolution batch is dead. The resolver can still be actively processing
# thousands of requests while progress metadata has not been committed yet.
#
# Therefore page_resolution uses a substantially longer, workload-adaptive
# stale threshold. Browser fallback remains shorter because each browser item
# already has its own hard watchdog in sitemap_page_resolver.py.
#
SUPERVISOR_POLL_SECONDS = max(
    2.0,
    float(os.getenv("WORKER_SUPERVISOR_POLL_SECONDS", "5")),
)
STARTUP_GRACE_SECONDS = max(
    30.0,
    float(os.getenv("WORKER_STARTUP_GRACE_SECONDS", "90")),
)

# Generic non-resolution stages.
DEFAULT_STALE_SECONDS = max(
    300.0,
    float(os.getenv("WORKER_RUN_STALE_SECONDS", "900")),
)

# Large HTTP sitemap resolution must not be killed after only five minutes.
PAGE_RESOLUTION_STALE_SECONDS = max(
    900.0,
    float(os.getenv("WORKER_PAGE_RESOLUTION_STALE_SECONDS", "1800")),
)
PAGE_RESOLUTION_MAX_STALE_SECONDS = max(
    PAGE_RESOLUTION_STALE_SECONDS,
    float(os.getenv("WORKER_PAGE_RESOLUTION_MAX_STALE_SECONDS", "21600")),
)
PAGE_RESOLUTION_PER_ITEM_GRACE_SECONDS = max(
    0.0,
    float(os.getenv("WORKER_PAGE_RESOLUTION_PER_ITEM_GRACE_SECONDS", "0.03")),
)

# Browser fallback already has bounded per-URL hard timeouts, so this is only
# an outer safety net for a whole-stage deadlock.
BROWSER_STALE_SECONDS = max(
    300.0,
    float(os.getenv("WORKER_BROWSER_STALE_SECONDS", "900")),
)

# Taxonomy / embedding / AI stages can legitimately be quiet for longer.
LONG_STAGE_STALE_SECONDS = max(
    900.0,
    float(os.getenv("WORKER_LONG_STAGE_STALE_SECONDS", "1800")),
)

MAX_AUTO_RECOVERIES = max(
    0,
    int(os.getenv("WORKER_AUTO_RECOVERY_MAX", "2")),
)
ORPHAN_SCAN_SECONDS = max(
    15.0,
    float(os.getenv("WORKER_ORPHAN_SCAN_SECONDS", "60")),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime | None:
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _run_model(kind: RunKind):
    return models.SitemapRun if kind in {"sitemap", "sitemap_recovery"} else models.Run


def _terminal_statuses(kind: RunKind) -> set[str]:
    if kind == "sitemap":
        return _TERMINAL_SITEMAP
    if kind == "sitemap_recovery":
        # Background recovery intentionally keeps the main run in a terminal,
        # exportable status. Completion is tracked in config_json instead.
        return {"failed", "cancelled"}
    return _TERMINAL_PAGE


def _progress_meta(row) -> dict:
    return dict((row.config_json or {}).get("_progress") or {})


def _resolution_meta(row) -> dict:
    return dict((row.config_json or {}).get("_sitemap_resolution") or {})


def _heartbeat_age_seconds(row) -> float | None:
    meta = _progress_meta(row)
    heartbeat = _as_utc(meta.get("heartbeat_at"))

    if heartbeat is None:
        heartbeat = _as_utc(getattr(row, "started_at", None))

    if heartbeat is None:
        heartbeat = _as_utc(getattr(row, "created_at", None))

    if heartbeat is None:
        return None

    return max(0.0, (_utcnow() - heartbeat).total_seconds())


def _phase_name(row) -> str:
    return str(getattr(row, "phase", None) or "").strip().casefold()


def _remaining_page_resolution_items(row) -> int:
    """Best-effort remaining HTTP-resolution workload.

    The sitemap job stores richer resolution counters in config_json. If those
    counters are not present yet, fall back to progress stage counters.
    """
    resolution = _resolution_meta(row)

    uncached_total = int(resolution.get("uncached_total") or 0)
    processed_uncached = int(resolution.get("processed_uncached") or 0)

    if uncached_total > 0:
        return max(0, uncached_total - processed_uncached)

    progress = _progress_meta(row)
    stage_total = int(progress.get("stage_total") or 0)
    stage_current = int(progress.get("stage_current") or 0)

    if stage_total > 0:
        return max(0, stage_total - stage_current)

    return 0


def _stale_threshold_for_phase(phase: str | None) -> float:
    """Base phase threshold used by tests and non-row callers."""
    value = str(phase or "").strip().casefold()

    if value == "page_resolution":
        return PAGE_RESOLUTION_STALE_SECONDS

    if value in {"browser_fallback", "impersonated_http_fallback"}:
        return BROWSER_STALE_SECONDS

    if any(
        token in value
        for token in (
            "taxonomy",
            "coverage",
            "embedding",
            "ai_",
            "finalizing",
            "recovery",
        )
    ):
        return LONG_STAGE_STALE_SECONDS

    return DEFAULT_STALE_SECONDS


def _stale_threshold_for_row(row) -> float:
    """Return a workload-aware stale threshold for one run row.

    Large sitemap HTTP resolution is intentionally adaptive. The previous
    fixed 300-second watchdog produced false failures while httpx was still
    visibly downloading pages.
    """
    phase = _phase_name(row)

    if phase != "page_resolution":
        return _stale_threshold_for_phase(phase)

    remaining = _remaining_page_resolution_items(row)

    adaptive = (
        PAGE_RESOLUTION_STALE_SECONDS
        + remaining * PAGE_RESOLUTION_PER_ITEM_GRACE_SECONDS
    )

    return min(
        PAGE_RESOLUTION_MAX_STALE_SECONDS,
        max(PAGE_RESOLUTION_STALE_SECONDS, adaptive),
    )


def _progress_signature(row) -> tuple:
    """Small DB-visible progress signature for supervisor diagnostics."""
    progress = _progress_meta(row)
    resolution = _resolution_meta(row)

    return (
        str(getattr(row, "status", "") or ""),
        str(getattr(row, "phase", "") or ""),
        progress.get("stage_current"),
        progress.get("stage_total"),
        progress.get("percent"),
        progress.get("overall_percent"),
        resolution.get("processed_uncached"),
        resolution.get("resolved_successfully"),
        resolution.get("resolution_failed"),
    )


def _recovery_state(row) -> dict:
    cfg = dict(row.config_json or {})
    return dict(cfg.get("_auto_recovery") or {})


def _set_recovery_state(
    row,
    *,
    attempts: int,
    reason: str,
    action: str,
) -> None:
    cfg = dict(row.config_json or {})
    state = dict(cfg.get("_auto_recovery") or {})

    state.update(
        {
            "attempts": attempts,
            "last_reason": reason,
            "last_action": action,
            "updated_at": _utcnow().isoformat(),
        }
    )

    cfg["_auto_recovery"] = state
    row.config_json = cfg


def _sanitize_terminal_run(kind: RunKind, run_id: int) -> None:
    db = SessionLocal()

    try:
        row = db.get(_run_model(kind), run_id)

        if not row:
            return

        if getattr(row, "analysis_json", None) is not None:
            row.analysis_json = sanitize_run_payload(
                row.analysis_json or {}
            )

        # Keep the public run contract clean of retired/legacy metadata.
        row.config_json = sanitize_run_payload(
            row.config_json or {}
        )

        db.commit()

    except Exception:
        db.rollback()
        logger.exception(
            "Could not sanitize %s run %s",
            kind,
            run_id,
        )

    finally:
        db.close()


def _cancel_requested(row) -> bool:
    if str(getattr(row, "status", "") or "").strip().casefold() == "cancelling":
        return True

    cfg = dict(getattr(row, "config_json", None) or {})
    control = dict(cfg.get("_control") or {})
    return bool(control.get("cancel_requested"))


def _mark_cancelled(kind: RunKind, run_id: int) -> None:
    """Persist cancellation only after the supervised child tree is stopped."""
    db = SessionLocal()
    try:
        row = db.get(_run_model(kind), run_id)
        if not row:
            return

        # Do not overwrite a result that genuinely reached a terminal state
        # before the supervisor was able to terminate it.
        if row.status in _terminal_statuses(kind) and row.status != "cancelled":
            return

        row.status = "cancelled"
        row.phase = "cancelled"
        row.error = None
        row.completed_at = _utcnow()

        cfg = dict(row.config_json or {})
        control = dict(cfg.get("_control") or {})
        control.update(
            {
                "cancel_requested": True,
                "cancelled_at": _utcnow().isoformat(),
                "cancel_state": "completed",
            }
        )
        cfg["_control"] = control
        row.config_json = cfg
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not finalize cancellation for %s run %s", kind, run_id)
    finally:
        db.close()


def _background_recovery_meta(row) -> dict:
    return dict((getattr(row, "config_json", None) or {}).get("_background_recovery") or {})


def _background_recovery_due(row) -> bool:
    meta = _background_recovery_meta(row)
    if not meta or not bool(meta.get("automatic", True)):
        return False
    if str(meta.get("state") or "").lower() not in {"queued", "cooling"}:
        return False
    due = _as_utc(meta.get("next_attempt_at"))
    return due is None or due <= _utcnow()


def claim_sitemap_recovery(run_id: int | None = None):
    """Claim one due automatic recovery slice without changing main run status.

    Historical terminal runs created before this patch are lazily enrolled once.
    This lets an existing Run 15 begin background recovery automatically after
    the worker restarts, without creating a new sitemap crawl/run.
    """
    if not bool(settings.sitemap_background_recovery_enabled):
        return None

    db = SessionLocal()
    try:
        eligible_statuses = {"completed", "completed_with_warnings", "partial"}
        if run_id is not None:
            rows = [db.get(models.SitemapRun, run_id)]
        else:
            rows = (
                db.query(models.SitemapRun)
                .filter(models.SitemapRun.status.in_(sorted(eligible_statuses)))
                .order_by(models.SitemapRun.id.desc())
                .all()
            )

        for row in rows:
            if not row or row.status not in eligible_statuses or _cancel_requested(row):
                continue

            cfg = dict(row.config_json or {})
            meta = dict(cfg.get("_background_recovery") or {})
            if not meta:
                from app.services.sitemap_recovery import _is_recoverable

                failed_rows = (
                    db.query(models.SitemapUrl)
                    .filter(
                        models.SitemapUrl.sitemap_run_id == row.id,
                        models.SitemapUrl.error.is_not(None),
                    )
                    .all()
                )
                recoverable = sum(1 for item in failed_rows if _is_recoverable(item))
                if recoverable:
                    meta = {
                        "version": 1,
                        "automatic": True,
                        "state": "queued",
                        "queued_at": _utcnow().isoformat(),
                        "next_attempt_at": _utcnow().isoformat(),
                        "base_status": row.status,
                        "base_phase": row.phase,
                        "recoverable_rows": int(recoverable),
                        "slices_completed": 0,
                        "bootstrapped_existing_run": True,
                    }
                else:
                    meta = {
                        "version": 1,
                        "automatic": True,
                        "state": "completed",
                        "completed_at": _utcnow().isoformat(),
                        "recoverable_rows": 0,
                        "slices_completed": 0,
                        "base_status": row.status,
                        "base_phase": row.phase,
                        "bootstrapped_existing_run": True,
                    }
                cfg["_background_recovery"] = meta
                row.config_json = cfg
                db.commit()

            if not _background_recovery_due(row):
                continue

            cfg = dict(row.config_json or {})
            meta = dict(cfg.get("_background_recovery") or {})
            meta.update({
                "state": "claimed",
                "claimed_at": _utcnow().isoformat(),
                "next_attempt_at": None,
            })
            cfg["_background_recovery"] = meta
            row.config_json = cfg
            db.commit()
            return row.id
        return None
    finally:
        db.close()


def _background_recovery_finished(row) -> bool:
    state = str(_background_recovery_meta(row).get("state") or "").lower()
    return state in {"queued", "cooling", "completed", "bounded_complete", "paused", "failed", "cancelled"}


def _requeue_background_recovery_after_crash(run_id: int, reason: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(models.SitemapRun, run_id)
        if not row:
            return
        cfg = dict(row.config_json or {})
        meta = dict(cfg.get("_background_recovery") or {})
        crashes = int(meta.get("worker_crashes") or 0) + 1
        delay = max(60.0, float(os.getenv("WORKER_BACKGROUND_RECOVERY_RETRY_SECONDS", "120")))
        meta.update({
            "state": "queued",
            "worker_crashes": crashes,
            "last_error": reason,
            "next_attempt_at": (_utcnow() + timedelta(seconds=delay)).isoformat(),
        })
        cfg["_background_recovery"] = meta
        row.config_json = cfg
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not requeue background sitemap recovery %s", run_id)
    finally:
        db.close()


def claim_page(run_id: int | None = None):
    db = SessionLocal()

    try:
        if run_id is not None:
            row = db.get(models.Run, run_id)

            if row and row.status in {
                "queued",
                "dispatch_failed",
                "failed",
            }:
                row.status = "claimed"
                db.commit()
                return row.id

            return None

        stmt = (
            select(models.Run)
            .where(models.Run.status == "queued")
            .order_by(models.Run.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        row = db.execute(stmt).scalars().first()

        if not row:
            return None

        row.status = "claimed"
        db.commit()
        return row.id

    finally:
        db.close()


def claim_sitemap(run_id: int | None = None):
    db = SessionLocal()

    try:
        if run_id is not None:
            row = db.get(models.SitemapRun, run_id)

            if row and row.status in {
                "queued",
                "dispatch_failed",
                "failed",
            }:
                row.status = "claimed"
                db.commit()
                return row.id

            return None

        stmt = (
            select(models.SitemapRun)
            .where(models.SitemapRun.status == "queued")
            .order_by(models.SitemapRun.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        row = db.execute(stmt).scalars().first()

        if not row:
            return None

        row.status = "claimed"
        db.commit()
        return row.id

    finally:
        db.close()


def _child_entry(kind: RunKind, run_id: int) -> None:
    """Run one analysis in an isolated child process.

    On POSIX the child becomes a new process-group leader. That allows the
    supervisor to terminate the whole run tree, including Playwright/Node/
    Chromium descendants, rather than leaving orphan processes behind.
    """
    if os.name == "posix":
        try:
            os.setsid()
        except Exception:
            logger.exception(
                "Could not create process group for %s run %s",
                kind,
                run_id,
            )

    if kind == "sitemap":
        from app.services.sitemap_job_runner import execute_sitemap_run

        asyncio.run(execute_sitemap_run(run_id))
    elif kind == "sitemap_recovery":
        from app.services.sitemap_recovery import recover_sitemap_run

        asyncio.run(recover_sitemap_run(run_id, background=True))
    else:
        from app.services.job_runner import execute_run

        asyncio.run(execute_run(run_id))


def _terminate_process(proc: mp.Process) -> None:
    """Terminate a supervised run and all descendants when possible."""
    if not proc.is_alive():
        proc.join(timeout=1)
        return

    # On macOS/Linux the child called setsid(), so pid == pgid.
    if os.name == "posix" and proc.pid:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception(
                "Could not SIGTERM process group %s",
                proc.pid,
            )
            try:
                proc.terminate()
            except Exception:
                pass
    else:
        try:
            proc.terminate()
        except Exception:
            pass

    proc.join(timeout=8)

    if not proc.is_alive():
        return

    if os.name == "posix" and proc.pid:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception(
                "Could not SIGKILL process group %s",
                proc.pid,
            )
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except Exception:
                    pass
    elif hasattr(proc, "kill"):
        try:
            proc.kill()
        except Exception:
            pass

    proc.join(timeout=3)


def _prepare_auto_retry(
    kind: RunKind,
    run_id: int,
    reason: str,
) -> bool:
    db = SessionLocal()

    try:
        row = db.get(_run_model(kind), run_id)

        if not row:
            return False

        if _cancel_requested(row):
            row.status = "cancelled"
            row.phase = "cancelled"
            row.error = None
            row.completed_at = _utcnow()
            cfg = dict(row.config_json or {})
            control = dict(cfg.get("_control") or {})
            control.update(
                {
                    "cancel_requested": True,
                    "cancelled_at": _utcnow().isoformat(),
                    "cancel_state": "cancelled_before_auto_retry",
                }
            )
            cfg["_control"] = control
            row.config_json = cfg
            db.commit()
            return False

        state = _recovery_state(row)
        attempts = int(state.get("attempts") or 0)

        if attempts >= MAX_AUTO_RECOVERIES:
            row.status = "failed"
            row.phase = "failed"
            row.error = (
                f"Automatic recovery exhausted after {attempts} "
                f"attempt(s). Last reason: {reason}"
            )
            row.completed_at = _utcnow()

            _set_recovery_state(
                row,
                attempts=attempts,
                reason=reason,
                action="recovery_exhausted",
            )

            db.commit()
            return False

        attempts += 1

        row.status = "claimed"
        row.phase = "auto_recovery"
        row.error = None

        _set_recovery_state(
            row,
            attempts=attempts,
            reason=reason,
            action="retry_same_run_from_cache",
        )

        db.commit()

        logger.warning(
            "Auto-recovering %s run %s (%s/%s): %s",
            kind,
            run_id,
            attempts,
            MAX_AUTO_RECOVERIES,
            reason,
        )

        return True

    except Exception:
        db.rollback()
        logger.exception(
            "Could not prepare auto recovery for %s run %s",
            kind,
            run_id,
        )
        return False

    finally:
        db.close()


def _execute_supervised(kind: RunKind, run_id: int) -> None:
    ctx = mp.get_context("spawn")

    while True:
        proc = ctx.Process(
            target=_child_entry,
            args=(kind, run_id),
            name=f"seo-{kind}-run-{run_id}",
            daemon=False,
        )

        proc.start()

        launched = time.monotonic()
        stale_reason: str | None = None
        cancel_requested = False

        last_signature = None
        last_signature_change = time.monotonic()

        while proc.is_alive():
            time.sleep(SUPERVISOR_POLL_SECONDS)

            db = SessionLocal()

            try:
                row = db.get(
                    _run_model(kind),
                    run_id,
                )

                if not row:
                    stale_reason = (
                        "run row disappeared while child was active"
                    )
                    break

                if _cancel_requested(row):
                    cancel_requested = True
                    break

                if kind == "sitemap_recovery":
                    if _background_recovery_finished(row):
                        break
                elif row.status in _terminal_statuses(kind):
                    break

                signature = _progress_signature(row)

                if signature != last_signature:
                    last_signature = signature
                    last_signature_change = time.monotonic()

                if (
                    time.monotonic() - launched
                    < STARTUP_GRACE_SECONDS
                ):
                    continue

                age = _heartbeat_age_seconds(row)
                threshold = _stale_threshold_for_row(row)

                # Require BOTH conditions:
                #   1. persisted heartbeat is stale
                #   2. persisted progress signature has not changed for the
                #      same adaptive threshold
                #
                # This prevents a transiently stale heartbeat from killing a
                # run whose DB-visible counters are still advancing.
                no_progress_for = (
                    time.monotonic()
                    - last_signature_change
                )

                if (
                    age is not None
                    and age > threshold
                    and no_progress_for > threshold
                ):
                    stale_reason = (
                        f"stale heartbeat for {age:.0f}s and no DB-visible "
                        f"progress for {no_progress_for:.0f}s in phase "
                        f"{getattr(row, 'phase', None)!r}; "
                        f"threshold={threshold:.0f}s"
                    )
                    break

            except Exception as exc:
                # SQLite can be briefly write-locked by the child while it
                # checkpoints. Supervisor read failure must never kill the
                # active run.
                logger.warning(
                    "Supervisor heartbeat read failed for %s run %s: %s",
                    kind,
                    run_id,
                    exc,
                )

            finally:
                db.close()

        if cancel_requested:
            logger.info(
                "Cancelling supervised %s run %s",
                kind,
                run_id,
            )
            _terminate_process(proc)
            _mark_cancelled(kind, run_id)
            _sanitize_terminal_run(kind, run_id)
            return

        if stale_reason:
            logger.error(
                "Terminating stale %s run %s: %s",
                kind,
                run_id,
                stale_reason,
            )

            _terminate_process(proc)

            if kind == "sitemap_recovery":
                _requeue_background_recovery_after_crash(run_id, stale_reason)
                return

            if _prepare_auto_retry(
                kind,
                run_id,
                stale_reason,
            ):
                continue

            _sanitize_terminal_run(
                kind,
                run_id,
            )
            return

        proc.join(timeout=10)

        if proc.is_alive():
            _terminate_process(proc)

        db = SessionLocal()

        try:
            row = db.get(
                _run_model(kind),
                run_id,
            )
            status = row.status if row else None
            cancel_after_exit = bool(row and _cancel_requested(row))
            background_done = bool(row and kind == "sitemap_recovery" and _background_recovery_finished(row))

        finally:
            db.close()

        if cancel_after_exit:
            _mark_cancelled(kind, run_id)
            _sanitize_terminal_run(kind, run_id)
            return

        if kind == "sitemap_recovery" and background_done:
            _sanitize_terminal_run("sitemap", run_id)
            return

        if status in _terminal_statuses(kind):
            _sanitize_terminal_run(
                kind,
                run_id,
            )
            return

        # Child exited/crashed without a terminal DB state.
        reason = (
            f"worker child exited unexpectedly with code "
            f"{proc.exitcode}; status={status!r}"
        )

        if kind == "sitemap_recovery":
            _requeue_background_recovery_after_crash(run_id, reason)
            return

        if _prepare_auto_retry(
            kind,
            run_id,
            reason,
        ):
            continue

        _sanitize_terminal_run(
            kind,
            run_id,
        )
        return


def _requeue_orphaned(kind: RunKind) -> int:
    """Recover live rows left behind by a dead supervisor/process."""
    db = SessionLocal()
    count = 0

    try:
        model = _run_model(kind)

        rows = (
            db.query(model)
            .filter(model.status.in_(["claimed", "running", "cancelling"]))
            .all()
        )

        for row in rows:
            if _cancel_requested(row):
                row.status = "cancelled"
                row.phase = "cancelled"
                row.error = None
                row.completed_at = _utcnow()
                cfg = dict(row.config_json or {})
                control = dict(cfg.get("_control") or {})
                control.update(
                    {
                        "cancel_requested": True,
                        "cancelled_at": _utcnow().isoformat(),
                        "cancel_state": "completed_after_worker_restart",
                    }
                )
                cfg["_control"] = control
                row.config_json = cfg
                continue

            age = _heartbeat_age_seconds(row)
            threshold = _stale_threshold_for_row(row)

            if age is None or age <= threshold:
                continue

            state = _recovery_state(row)
            attempts = int(state.get("attempts") or 0)

            if attempts >= MAX_AUTO_RECOVERIES:
                row.status = "failed"
                row.phase = "failed"
                row.error = (
                    "Orphaned run recovery exhausted after "
                    f"{attempts} attempt(s); "
                    f"last heartbeat age={age:.0f}s; "
                    f"threshold={threshold:.0f}s"
                )
                row.completed_at = _utcnow()

                _set_recovery_state(
                    row,
                    attempts=attempts,
                    reason=row.error,
                    action="orphan_recovery_exhausted",
                )

            else:
                attempts += 1

                row.status = "queued"
                row.phase = "auto_recovery_queued"
                row.error = None

                _set_recovery_state(
                    row,
                    attempts=attempts,
                    reason=(
                        "orphaned live run; heartbeat "
                        f"age={age:.0f}s; "
                        f"threshold={threshold:.0f}s"
                    ),
                    action="requeued_after_worker_restart",
                )

                count += 1

        db.commit()

    except Exception:
        db.rollback()
        logger.exception(
            "Orphan scan failed for %s runs",
            kind,
        )

    finally:
        db.close()

    return count


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--once",
        action="store_true",
    )
    parser.add_argument(
        "--run-id",
        type=int,
    )
    parser.add_argument(
        "--sitemap-run-id",
        type=int,
    )

    args = parser.parse_args()

    if args.run_id and args.sitemap_run_id:
        raise SystemExit(
            "Use only one of --run-id or --sitemap-run-id"
        )

    recovered = (
        _requeue_orphaned("page")
        + _requeue_orphaned("sitemap")
    )

    if recovered:
        logger.warning(
            "Requeued %s orphaned run(s) at worker startup",
            recovered,
        )

    last_orphan_scan = time.monotonic()

    while True:
        if (
            time.monotonic() - last_orphan_scan
            >= ORPHAN_SCAN_SECONDS
        ):
            _requeue_orphaned("page")
            _requeue_orphaned("sitemap")
            last_orphan_scan = time.monotonic()

        if args.sitemap_run_id is not None:
            claimed = claim_sitemap(
                args.sitemap_run_id
            )
            kind: RunKind = "sitemap"
            if not claimed:
                claimed = claim_sitemap_recovery(args.sitemap_run_id)
                kind = "sitemap_recovery"

        elif args.run_id is not None:
            claimed = claim_page(
                args.run_id
            )
            kind = "page"

        else:
            claimed = claim_page()
            kind = "page"

            if not claimed:
                claimed = claim_sitemap()
                kind = "sitemap"

            if not claimed:
                claimed = claim_sitemap_recovery()
                kind = "sitemap_recovery"

        if claimed:
            logger.info(
                "Executing supervised %s run %s",
                kind,
                claimed,
            )

            try:
                _execute_supervised(
                    kind,
                    claimed,
                )

            except KeyboardInterrupt:
                raise

            except Exception:
                logger.exception(
                    "Supervised %s run %s failed",
                    kind,
                    claimed,
                )

            if (
                args.once
                or args.run_id
                or args.sitemap_run_id
            ):
                return

        elif (
            args.once
            or args.run_id
            or args.sitemap_run_id
        ):
            logger.info(
                "No eligible run found"
            )
            return

        else:
            time.sleep(
                settings.worker_poll_seconds
            )


if __name__ == "__main__":
    main()

################################################################################
# BACKEND — PAGE ANALYSIS PIPELINE
################################################################################
