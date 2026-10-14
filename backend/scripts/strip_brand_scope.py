from __future__ import annotations

import argparse

from app import models
from app.database import SessionLocal
from app.services.analysis_contract import sanitize_run_payload


def _update_rows(db, model, ids: set[int] | None) -> int:
    query = db.query(model)
    if ids:
        query = query.filter(model.id.in_(sorted(ids)))
    count = 0
    for row in query.yield_per(100):
        changed = False
        analysis = sanitize_run_payload(row.analysis_json or {})
        config = sanitize_run_payload(row.config_json or {})
        if analysis != (row.analysis_json or {}):
            row.analysis_json = analysis
            changed = True
        if config != (row.config_json or {}):
            row.config_json = config
            changed = True
        if changed:
            count += 1
            if count % 100 == 0:
                db.commit()
    db.commit()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove legacy Brand Scope fields from persisted Page/Sitemap analysis snapshots."
    )
    parser.add_argument("--run-id", type=int, action="append", default=[])
    parser.add_argument("--sitemap-run-id", type=int, action="append", default=[])
    parser.add_argument(
        "--all",
        action="store_true",
        help="Clean every historical Page Analysis and Sitemap Analysis run.",
    )
    args = parser.parse_args()

    if not args.all and not args.run_id and not args.sitemap_run_id:
        parser.error("Use --all or provide --run-id / --sitemap-run-id")

    db = SessionLocal()
    try:
        page_ids = None if args.all else set(args.run_id)
        sitemap_ids = None if args.all else set(args.sitemap_run_id)

        page_count = _update_rows(db, models.Run, page_ids) if (args.all or args.run_id) else 0
        sitemap_count = (
            _update_rows(db, models.SitemapRun, sitemap_ids)
            if (args.all or args.sitemap_run_id)
            else 0
        )
        print(f"Page runs cleaned: {page_count}")
        print(f"Sitemap runs cleaned: {sitemap_count}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
