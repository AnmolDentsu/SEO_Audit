from __future__ import annotations

import logging
import time
from typing import Literal

from app.config import get_settings

from app.worker import (
    ORPHAN_SCAN_SECONDS,
    _execute_supervised,
    _requeue_orphaned,
    claim_page,
    claim_sitemap,
)


settings = get_settings()

logger = logging.getLogger(
    "dedicated-worker"
)


WorkerKind = Literal[
    "page",
    "sitemap",
]


def run_worker(
    kind: WorkerKind,
    *,
    once: bool = False,
    requested_run_id: int | None = None,
) -> None:
    if kind not in {
        "page",
        "sitemap",
    }:
        raise ValueError(
            "kind must be page or sitemap"
        )

    recovered = _requeue_orphaned(
        kind
    )

    if recovered:
        logger.warning(
            "Requeued %s orphaned %s run(s) "
            "at worker startup",
            recovered,
            kind,
        )

    last_orphan_scan = (
        time.monotonic()
    )

    while True:
        if (
            time.monotonic()
            - last_orphan_scan
            >= ORPHAN_SCAN_SECONDS
        ):
            _requeue_orphaned(
                kind
            )

            last_orphan_scan = (
                time.monotonic()
            )

        if kind == "page":
            claimed = claim_page(
                requested_run_id
            )

        else:
            claimed = claim_sitemap(
                requested_run_id
            )

        if claimed:
            logger.info(
                "Executing dedicated supervised "
                "%s run %s",
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
                    "Dedicated %s run %s failed",
                    kind,
                    claimed,
                )

            if (
                once
                or requested_run_id
                is not None
            ):
                return

            continue

        if (
            once
            or requested_run_id
            is not None
        ):
            logger.info(
                "No eligible %s run found",
                kind,
            )

            return

        time.sleep(
            max(
                0.5,
                float(
                    settings.worker_poll_seconds
                ),
            )
        )