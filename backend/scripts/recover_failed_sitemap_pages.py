from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from app import models
from app.database import SessionLocal
from app.services.sitemap_recovery import recover_sitemap_run


def _now():
    return datetime.now(timezone.utc)


def prepare_recovery(run_id: int, *, browser: bool) -> int:
    db = SessionLocal()
    try:
        run = db.get(models.SitemapRun, run_id)
        if not run:
            raise SystemExit(f"Sitemap run #{run_id} not found")
        if run.status in {"queued", "claimed", "running", "cancelling"}:
            raise SystemExit(f"Sitemap run #{run_id} is currently active ({run.status})")

        failed_count = (
            db.query(models.SitemapUrl)
            .filter(
                models.SitemapUrl.sitemap_run_id == run_id,
                models.SitemapUrl.error.is_not(None),
            )
            .count()
        )
        if not failed_count:
            print(f"Run #{run_id} has no persisted failed URLs. Nothing to recover.")
            return 0

        cfg = dict(run.config_json or {})
        cfg["_recovery_only"] = {
            "requested_at": _now().isoformat(),
            "failed_rows_at_request": int(failed_count),
            "mode": "failed_urls_only",
            "browser": bool(browser),
            "requested_by": "cli",
        }
        control = dict(cfg.get("_control") or {})
        control["cancel_requested"] = False
        cfg["_control"] = control
        cfg.pop("_progress", None)
        run.config_json = cfg
        run.status = "claimed"
        run.phase = "recovery_queued"
        run.error = None
        run.started_at = None
        run.completed_at = None
        run.progress_current = 0
        run.progress_total = 1000
        db.commit()
        print(
            f"Run #{run_id}: prepared recovery of {failed_count:,} persisted failed URL(s). "
            "No sitemap rediscovery and no refetch of successful URLs."
        )
        return int(failed_count)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Recover only persisted failed SitemapUrl rows, then rebuild derived "
            "comparison/opportunity outputs without a full sitemap crawl."
        )
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument(
        "--no-browser",
        dest="browser",
        action="store_false",
        help="Use adaptive HTTP/curl-cffi recovery only; skip Chromium last resort.",
    )
    parser.set_defaults(browser=True)
    args = parser.parse_args()

    if prepare_recovery(args.run_id, browser=args.browser):
        asyncio.run(recover_sitemap_run(args.run_id))


if __name__ == "__main__":
    main()
