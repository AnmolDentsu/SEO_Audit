from __future__ import annotations

import argparse
from datetime import datetime, timezone

from app import models
from app.database import SessionLocal
from app.services.sitemap_result_views import compact_analysis_payload, result_quality


def now_utc():
    return datetime.now(timezone.utc)


def main():
    parser = argparse.ArgumentParser(
        description="Compact an already-completed Sitemap Intelligence run without recrawling or recomputing coverage."
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument(
        "--mark-quality-warning",
        action="store_true",
        help="If quality diagnostics warn, change completed -> completed_with_warnings.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        run = db.get(models.SitemapRun, args.run_id)
        if not run:
            raise SystemExit(f"Sitemap run #{args.run_id} not found")

        opportunity_count = (
            db.query(models.SitemapOpportunity)
            .filter_by(sitemap_run_id=args.run_id)
            .count()
        )
        analysis = dict(run.analysis_json or {})
        before_keys = sorted(analysis.keys())
        before_counts = {
            "topic_taxonomy": len(analysis.get("topic_taxonomy") or []),
            "topic_coverage": len(analysis.get("topic_coverage_matrix") or []),
            "head_to_head": len(analysis.get("head_to_head") or []),
            "missing_categories": len(analysis.get("missing_categories") or []),
            "content_opportunities_json": len(analysis.get("content_opportunities") or []),
            "db_opportunities": opportunity_count,
        }

        quality = result_quality(db, args.run_id, opportunity_count=opportunity_count)
        compact = compact_analysis_payload(
            analysis,
            quality=quality,
            opportunity_count=opportunity_count,
        )

        run.analysis_json = compact
        cfg = dict(run.config_json or {})
        cfg["_result_summary"] = {
            "comparison_status": compact.get("comparison_status") or {},
            "quality": quality,
            "result_counts": compact.get("result_counts") or {},
        }
        run.config_json = cfg

        if args.mark_quality_warning and run.status == "completed" and quality.get("status") == "warning":
            run.status = "completed_with_warnings"
            run.phase = "quality_warning"
            run.error = " ".join(quality.get("warnings") or [])
            # completed_at remains the original completion time.

        db.commit()

        print(f"Run #{args.run_id} compacted successfully")
        print("Before keys:", before_keys)
        print("Before counts:", before_counts)
        print("After keys:", sorted(compact.keys()))
        print("Quality:", quality)
        print("Final status:", run.status)
        print("No crawl, HTTP resolution, browser fallback, embedding or AI call was executed.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
