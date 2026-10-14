from __future__ import annotations

import os
from datetime import datetime, timezone
from statistics import median
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app import models


PAGE_TERMINAL = {
    "completed",
    "failed",
    "dispatch_failed",
    "cancelled",
    "partial",
    "paused",
}

SITEMAP_TERMINAL = {
    "completed",
    "completed_with_warnings",
    "partial",
    "failed",
    "dispatch_failed",
    "cancelled",
    "paused",
}

ACTIVE = {
    "claimed",
    "running",
    "cancelling",
}

QUEUED = {
    "queued",
}


def _utcnow() -> datetime:
    return datetime.now(
        timezone.utc
    )


def _as_utc(
    value,
) -> datetime | None:
    if not value:
        return None

    if isinstance(
        value,
        datetime,
    ):
        dt = value

    else:
        try:
            dt = datetime.fromisoformat(
                str(value).replace(
                    "Z",
                    "+00:00",
                )
            )
        except Exception:
            return None

    if dt.tzinfo:
        return dt

    return dt.replace(
        tzinfo=timezone.utc
    )


def _seconds_between(
    start,
    end,
) -> float | None:
    start_dt = _as_utc(start)
    end_dt = _as_utc(end)

    if (
        start_dt is None
        or end_dt is None
    ):
        return None

    return max(
        0.0,
        (
            end_dt
            - start_dt
        ).total_seconds(),
    )


def _duration(
    row,
) -> float | None:
    return _seconds_between(
        row.started_at
        or row.created_at,
        row.completed_at,
    )


def _snapshot(
    row,
) -> dict:
    return dict(
        (
            row.config_json
            or {}
        ).get(
            "_input_snapshot"
        )
        or {}
    )


def _page_units(
    row,
) -> int:
    targets = list(
        _snapshot(row).get(
            "targets"
        )
        or []
    )

    return max(
        1,
        len(targets)
        or int(
            row.progress_total
            or 1
        ),
    )


def _sitemap_units(
    row,
) -> int | None:
    config = dict(
        row.config_json
        or {}
    )

    resolution = dict(
        config.get(
            "_sitemap_resolution"
        )
        or {}
    )

    value = resolution.get(
        "resolution_candidates"
    )

    try:
        parsed = int(value)
    except (
        TypeError,
        ValueError,
    ):
        parsed = 0

    if parsed > 0:
        return parsed

    return None


def _domains(
    row,
) -> frozenset[str]:
    sources = list(
        _snapshot(row).get(
            "sources"
        )
        or []
    )

    hosts: set[str] = set()

    for item in sources:
        host = (
            urlparse(
                str(
                    item.get(
                        "root_url"
                    )
                    or ""
                )
            ).hostname
            or ""
        ).lower()

        if host:
            hosts.add(
                host.removeprefix(
                    "www."
                )
            )

    return frozenset(hosts)


def _history(
    db: Session,
    model,
    limit: int,
) -> list:
    terminal = (
        PAGE_TERMINAL
        if model is models.Run
        else SITEMAP_TERMINAL
    )

    return (
        db.query(model)
        .filter(
            model.status.in_(
                terminal
            ),
            model.started_at.isnot(
                None
            ),
            model.completed_at.isnot(
                None
            ),
        )
        .order_by(
            model.completed_at.desc(),
            model.id.desc(),
        )
        .limit(limit)
        .all()
    )


def _band(
    seconds: float,
    confidence: str,
) -> tuple[int, int]:
    seconds = max(
        0.0,
        float(seconds),
    )

    if confidence == "HIGH":
        low_factor = 0.85
        high_factor = 1.20

    elif confidence == "MEDIUM":
        low_factor = 0.70
        high_factor = 1.40

    else:
        low_factor = 0.60
        high_factor = 1.40

    low = int(
        seconds
        * low_factor
    )

    high = int(
        seconds
        * high_factor
    )

    return (
        max(
            0,
            low,
        ),
        max(
            low,
            high,
        ),
    )


# ---------------------------------------------------------------------------
# PAGE ANALYSIS ESTIMATE
# ---------------------------------------------------------------------------

def _estimate_total_page(
    row,
    history: list,
) -> tuple[
    float,
    str,
    str,
]:
    units = _page_units(row)

    per_unit: list[float] = []
    similar: list[float] = []

    for old in history:
        duration = _duration(old)

        if (
            duration is None
            or duration <= 0
        ):
            continue

        old_units = _page_units(
            old
        )

        per_unit.append(
            duration
            / old_units
        )

        tolerance = max(
            1,
            int(
                units
                * 0.5
            ),
        )

        if (
            abs(
                old_units
                - units
            )
            <= tolerance
        ):
            similar.append(
                duration
            )

    if len(similar) >= 5:
        return (
            median(similar),
            "HIGH",
            (
                "median of "
                f"{len(similar)} "
                "similar page runs"
            ),
        )

    if len(per_unit) >= 3:
        return (
            max(
                20.0,
                median(
                    per_unit
                )
                * units,
            ),
            "MEDIUM",
            (
                "median seconds/URL "
                f"from {len(per_unit)} "
                "page runs"
            ),
        )

    if similar:
        return (
            median(similar),
            "MEDIUM",
            (
                f"median of "
                f"{len(similar)} "
                "similar page run(s)"
            ),
        )

    default = float(
        os.getenv(
            "QUEUE_DEFAULT_PAGE_SECONDS",
            "300",
        )
    )

    return (
        default,
        "LOW",
        (
            "default page-analysis "
            "estimate; insufficient history"
        ),
    )


# ---------------------------------------------------------------------------
# SITEMAP ESTIMATE
# ---------------------------------------------------------------------------

def _estimate_total_sitemap(
    row,
    history: list,
) -> tuple[
    float,
    str,
    str,
]:
    units = _sitemap_units(
        row
    )

    domains = _domains(
        row
    )

    per_url: list[float] = []
    same_domain: list[float] = []
    general: list[float] = []

    for old in history:
        duration = _duration(old)

        if (
            duration is None
            or duration <= 0
        ):
            continue

        general.append(
            duration
        )

        old_units = _sitemap_units(
            old
        )

        if (
            old_units
            and old_units > 0
        ):
            per_url.append(
                duration
                / old_units
            )

        old_domains = _domains(
            old
        )

        if (
            domains
            and old_domains
            and domains
            & old_domains
        ):
            same_domain.append(
                duration
            )

    # Best estimate:
    # actual discovered URL count.
    if (
        units
        and len(per_url) >= 3
    ):
        confidence = (
            "HIGH"
            if len(per_url) >= 5
            else "MEDIUM"
        )

        return (
            max(
                60.0,
                median(
                    per_url
                )
                * units,
            ),
            confidence,
            (
                "URL-count model using "
                f"{len(per_url)} completed "
                "sitemap runs"
            ),
        )

    # Before discovery:
    # same-domain history.
    if len(same_domain) >= 3:
        return (
            median(
                same_domain
            ),
            "MEDIUM",
            (
                "median of "
                f"{len(same_domain)} "
                "runs sharing source domain(s)"
            ),
        )

    if same_domain:
        return (
            median(
                same_domain
            ),
            "MEDIUM",
            (
                f"{len(same_domain)} "
                "prior run(s) sharing "
                "source domain(s)"
            ),
        )

    if len(general) >= 5:
        return (
            median(
                general
            ),
            "LOW",
            (
                "global median of "
                f"{len(general)} "
                "sitemap runs"
            ),
        )

    # 50-minute central estimate.
    #
    # LOW confidence band becomes approximately:
    # 30–70 minutes.
    default = float(
        os.getenv(
            "QUEUE_DEFAULT_SITEMAP_SECONDS",
            "3000",
        )
    )

    return (
        default,
        "LOW",
        (
            "new-domain/default sitemap "
            "estimate; URL inventory "
            "not known yet"
        ),
    )


def _estimate_total(
    db: Session,
    kind: str,
    row,
) -> tuple[
    float,
    str,
    str,
]:
    limit = max(
        5,
        int(
            os.getenv(
                "QUEUE_HISTORY_LIMIT",
                "30",
            )
        ),
    )

    model = (
        models.Run
        if kind == "page"
        else models.SitemapRun
    )

    history = [
        old
        for old in _history(
            db,
            model,
            limit,
        )
        if old.id != row.id
    ]

    if kind == "page":
        return _estimate_total_page(
            row,
            history,
        )

    return _estimate_total_sitemap(
        row,
        history,
    )


# ---------------------------------------------------------------------------
# ACTIVE JOB REMAINING ETA
# ---------------------------------------------------------------------------

def _active_remaining(
    db: Session,
    kind: str,
    row,
) -> tuple[
    float,
    str,
    str,
]:
    config = dict(
        row.config_json
        or {}
    )

    progress = dict(
        config.get(
            "_progress"
        )
        or {}
    )

    try:
        backend_eta = float(
            progress.get(
                "eta_seconds"
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        backend_eta = 0.0

    if backend_eta > 0:
        return (
            backend_eta,
            "HIGH",
            "active-run backend ETA",
        )

    try:
        elapsed = float(
            progress.get(
                "elapsed_seconds"
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        elapsed = 0.0

    try:
        percent = float(
            progress.get(
                "overall_percent",
                progress.get(
                    "percent"
                ),
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        percent = 0.0

    if (
        elapsed > 0
        and 5 <= percent < 100
    ):
        total = (
            elapsed
            / (
                percent
                / 100.0
            )
        )

        remaining = max(
            0.0,
            total
            - elapsed,
        )

        recovery = dict(
            config.get(
                "_recovery_state"
            )
            or {}
        )

        cooldowns: list[float] = []

        for raw in dict(
            recovery.get(
                "hosts"
            )
            or {}
        ).values():

            host_state = dict(
                raw
                or {}
            )

            until = _as_utc(
                host_state.get(
                    "cooldown_until"
                )
            )

            if until:
                cooldowns.append(
                    max(
                        0.0,
                        (
                            until
                            - _utcnow()
                        ).total_seconds(),
                    )
                )

        if cooldowns:
            remaining = max(
                remaining,
                max(
                    cooldowns
                ),
            )

            return (
                remaining,
                "MEDIUM",
                (
                    "progress-derived ETA "
                    "including recovery cooldown"
                ),
            )

        return (
            remaining,
            "MEDIUM",
            "progress-derived active ETA",
        )

    # If progress percentage is not usable,
    # still respect recovery cooldown.
    recovery = dict(
        config.get(
            "_recovery_state"
        )
        or {}
    )

    cooldowns: list[float] = []

    for raw in dict(
        recovery.get(
            "hosts"
        )
        or {}
    ).values():

        host_state = dict(
            raw
            or {}
        )

        until = _as_utc(
            host_state.get(
                "cooldown_until"
            )
        )

        if until:
            cooldowns.append(
                max(
                    0.0,
                    (
                        until
                        - _utcnow()
                    ).total_seconds(),
                )
            )

    total_estimate, confidence, basis = (
        _estimate_total(
            db,
            kind,
            row,
        )
    )

    elapsed_live = (
        _seconds_between(
            row.started_at,
            _utcnow(),
        )
        or 0.0
    )

    fallback = max(
        60.0,
        total_estimate
        - elapsed_live,
    )

    if cooldowns:
        fallback = max(
            fallback,
            max(
                cooldowns
            ),
        )

        return (
            fallback,
            "LOW",
            (
                f"{basis}; "
                "recovery cooldown included"
            ),
        )

    return (
        fallback,
        confidence,
        basis,
    )


def _capacity(
    kind: str,
) -> int:
    env_name = (
        "PAGE_WORKER_CAPACITY"
        if kind == "page"
        else "SITEMAP_WORKER_CAPACITY"
    )

    return max(
        1,
        int(
            os.getenv(
                env_name,
                "1",
            )
        ),
    )


# ---------------------------------------------------------------------------
# PUBLIC QUEUE STATUS
# ---------------------------------------------------------------------------

def queue_status(
    db: Session,
    kind: str,
    run_id: int | None = None,
) -> dict:
    kind = (
        str(
            kind
            or ""
        )
        .strip()
        .lower()
    )

    if kind not in {
        "page",
        "sitemap",
    }:
        raise ValueError(
            "kind must be 'page' or 'sitemap'"
        )

    model = (
        models.Run
        if kind == "page"
        else models.SitemapRun
    )

    capacity = _capacity(
        kind
    )

    target = (
        db.get(
            model,
            run_id,
        )
        if run_id is not None
        else None
    )

    if (
        run_id is not None
        and target is None
    ):
        raise LookupError(
            f"{kind} run {run_id} not found"
        )

    active = (
        db.query(model)
        .filter(
            model.status.in_(
                ACTIVE
            )
        )
        .order_by(
            model.started_at,
            model.id,
        )
        .all()
    )

    queued = (
        db.query(model)
        .filter(
            model.status.in_(
                QUEUED
            )
        )
        .order_by(
            model.created_at,
            model.id,
        )
        .all()
    )

    # Each configured worker is one independent lane.
    low_lanes = [
        0.0
        for _ in range(
            capacity
        )
    ]

    high_lanes = [
        0.0
        for _ in range(
            capacity
        )
    ]

    active_details = []

    for index, row in enumerate(
        active
    ):
        (
            remaining,
            confidence,
            basis,
        ) = _active_remaining(
            db,
            kind,
            row,
        )

        low, high = _band(
            remaining,
            confidence,
        )

        lane = (
            index
            % capacity
        )

        low_lanes[
            lane
        ] += low

        high_lanes[
            lane
        ] += high

        active_details.append(
            {
                "run_id": row.id,
                "status": row.status,
                "phase": row.phase,
                "remaining_seconds": {
                    "low": low,
                    "high": high,
                },
                "confidence": confidence,
                "basis": basis,
            }
        )

    queue_position = None
    jobs_ahead = None
    estimated_start = None
    estimate_confidence = "LOW"
    estimate_basis = None

    for index, row in enumerate(
        queued
    ):
        low_lane = min(
            range(
                capacity
            ),
            key=lambda lane: (
                low_lanes[
                    lane
                ]
            ),
        )

        high_lane = min(
            range(
                capacity
            ),
            key=lambda lane: (
                high_lanes[
                    lane
                ]
            ),
        )

        wait_low = int(
            low_lanes[
                low_lane
            ]
        )

        wait_high = int(
            high_lanes[
                high_lane
            ]
        )

        (
            total,
            confidence,
            basis,
        ) = _estimate_total(
            db,
            kind,
            row,
        )

        duration_low, duration_high = (
            _band(
                total,
                confidence,
            )
        )

        if (
            target is not None
            and row.id
            == target.id
        ):
            queue_position = (
                index
                + 1
            )

            jobs_ahead = (
                len(active)
                + index
            )

            estimated_start = {
                "low": wait_low,
                "high": max(
                    wait_low,
                    wait_high,
                ),
            }

            estimate_confidence = (
                confidence
            )

            estimate_basis = basis
            break

        low_lanes[
            low_lane
        ] += duration_low

        high_lanes[
            high_lane
        ] += duration_high

    estimated_completion = None

    if (
        target is not None
        and target.status in ACTIVE
    ):
        (
            remaining,
            confidence,
            basis,
        ) = _active_remaining(
            db,
            kind,
            target,
        )

        low, high = _band(
            remaining,
            confidence,
        )

        estimated_start = {
            "low": 0,
            "high": 0,
        }

        estimated_completion = {
            "low": low,
            "high": high,
        }

        queue_position = 0
        jobs_ahead = 0

        estimate_confidence = (
            confidence
        )

        estimate_basis = basis

    if (
        target is not None
        and target.status
        not in (
            ACTIVE
            | QUEUED
        )
    ):
        estimated_start = {
            "low": 0,
            "high": 0,
        }

        queue_position = None
        jobs_ahead = 0

        estimate_confidence = (
            "HIGH"
        )

        estimate_basis = (
            "run is terminal "
            f"({target.status})"
        )

    return {
        "kind": kind,

        "run_id": (
            target.id
            if target is not None
            else None
        ),

        "run_status": (
            target.status
            if target is not None
            else None
        ),

        "worker_capacity": capacity,

        "active_count": len(
            active
        ),

        "queued_count": len(
            queued
        ),

        "queue_position": (
            queue_position
        ),

        "jobs_ahead": (
            jobs_ahead
        ),

        "estimated_start_seconds": (
            estimated_start
        ),

        "estimated_completion_seconds": (
            estimated_completion
        ),

        "confidence": (
            estimate_confidence
        ),

        "basis": (
            estimate_basis
        ),

        "active_runs": (
            active_details
        ),

        "updated_at": (
            _utcnow().isoformat()
        ),
    }


# ---------------------------------------------------------------------------
# QUEUE ABUSE LIMIT
# ---------------------------------------------------------------------------

def enforce_project_backlog(
    db: Session,
    kind: str,
    project_id: int,
) -> None:
    kind = (
        kind
        .strip()
        .lower()
    )

    if kind == "page":
        model = models.Run

        limit = max(
            1,
            int(
                os.getenv(
                    "QUEUE_MAX_PAGE_BACKLOG_PER_PROJECT",
                    "20",
                )
            ),
        )

    elif kind == "sitemap":
        model = models.SitemapRun

        limit = max(
            1,
            int(
                os.getenv(
                    "QUEUE_MAX_SITEMAP_BACKLOG_PER_PROJECT",
                    "10",
                )
            ),
        )

    else:
        raise ValueError(
            "Unknown queue kind"
        )

    pending = (
        db.query(model)
        .filter(
            model.project_id
            == project_id,

            model.status.in_(
                ACTIVE
                | QUEUED
            ),
        )
        .count()
    )

    if pending >= limit:
        raise ValueError(
            "This project already has "
            f"{pending} pending {kind} job(s); "
            f"the safety limit is {limit}. "
            "Wait for or cancel an existing job."
        )