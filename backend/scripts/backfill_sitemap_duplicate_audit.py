from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import delete

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.sitemap_duplicate_audit import (
    add_cross_source_duplicate,
    add_discovery_duplicates,
    rebuild_persisted_collision_audit,
)
from app.services.sitemap_fetcher import discover_sitemap_inventory, normalize_url

settings = get_settings()


def _now():
    return datetime.now(timezone.utc)


def _sources_for_run(db, run: models.SitemapRun):
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


async def backfill(run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.get(models.SitemapRun, run_id)
        if not run:
            raise RuntimeError(f"Sitemap run {run_id} not found")

        audit_meta = dict((run.config_json or {}).get("_duplicate_audit") or {})
        if audit_meta.get("mode") == "native":
            raise RuntimeError(
                "This run already contains native duplicate evidence; historical XML backfill is not required."
            )

        sources = _sources_for_run(db, run)
        if not sources:
            raise RuntimeError("No sitemap sources are available for this historical run")

        # Only replace rows created by this backfill stage. Native evidence from
        # newer runs is never deleted by this command.
        db.execute(
            delete(models.SitemapDuplicate).where(
                models.SitemapDuplicate.sitemap_run_id == run_id,
                models.SitemapDuplicate.audit_stage.in_(
                    ["historical_xml_backfill", "historical_persisted_refresh"]
                ),
            )
        )
        db.commit()

        max_urls = int(
            (run.config_json or {}).get("max_urls_per_source")
            or settings.sitemap_max_urls_per_source
        )

        seen_global: dict[str, tuple[object, object]] = {}
        exact_or_normalized = 0
        cross_source = 0

        for source in sources:
            print(f"Re-reading sitemap XML only: {source.label} -> {source.root_url}")
            entries, issues, stats = await discover_sitemap_inventory(
                source.root_url,
                max_urls=max_urls,
            )
            duplicate_evidence = list(stats.pop("duplicate_evidence", []) or [])
            exact_or_normalized += add_discovery_duplicates(
                db,
                run_id,
                source,
                duplicate_evidence,
                audit_stage="historical_xml_backfill",
            )

            for entry in entries:
                key = normalize_url(entry.url)
                first = seen_global.get(key)
                if first is None:
                    seen_global[key] = (source, entry)
                    continue
                first_source, first_entry = first
                add_cross_source_duplicate(
                    db,
                    run_id,
                    first_source,
                    first_entry,
                    source,
                    entry,
                    audit_stage="historical_xml_backfill",
                )
                cross_source += 1

            db.commit()
            errors = [x for x in issues if (x.get("severity") or "error") == "error"]
            print(
                f"  unique={len(entries):,} "
                f"duplicates={len(duplicate_evidence):,} "
                f"xml_errors={len(errors):,}"
            )

        persisted = (
            db.query(models.SitemapUrl)
            .filter(models.SitemapUrl.sitemap_run_id == run_id)
            .order_by(models.SitemapUrl.id)
            .all()
        )
        persisted_collisions = rebuild_persisted_collision_audit(
            db,
            run_id,
            persisted,
            audit_stage="historical_persisted_refresh",
        )

        cfg = dict(run.config_json or {})
        cfg["_duplicate_audit"] = {
            "version": 1,
            "mode": "historical_xml_backfill",
            "completed_at": _now().isoformat(),
            "xml_duplicate_rows": int(exact_or_normalized),
            "cross_source_rows": int(cross_source),
            "persisted_collision_rows": int(persisted_collisions),
            "limitation": (
                "Historical XML duplicates were reconstructed exactly from the current sitemap XML. "
                "Canonical/redirect losers discarded by the pre-audit pipeline cannot be recreated "
                "unless they are still inferable from persisted SitemapUrl rows."
            ),
        }
        run.config_json = cfg
        db.commit()

        total = (
            db.query(models.SitemapDuplicate)
            .filter(models.SitemapDuplicate.sitemap_run_id == run_id)
            .count()
        )
        print()
        print(f"Run {run_id} duplicate audit backfill complete")
        print(f"Persisted duplicate audit rows: {total:,}")
        print("No page-content recrawl was performed.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Re-read sitemap XML for a historical Sitemap Intelligence run and "
            "persist exact duplicate URL evidence without recrawling page content."
        )
    )
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(backfill(args.run_id))


if __name__ == "__main__":
    main()
