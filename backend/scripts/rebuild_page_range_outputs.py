#!/usr/bin/env python3
"""Rebuild Page Analysis opportunity outputs from persisted run data only.

This script does NOT crawl, fetch, open Chromium, or recalculate keyword density.
It reuses existing PageResult and DensityResult rows for a completed Page Analysis
run, rebuilds deterministic Opportunity rows with the current benchmark logic,
and refreshes only analysis_json['content_opportunities'] while preserving the
run's existing section/FAQ/keyword-placement analysis.

Use this after changing P0/P1 benchmark range semantics if you want a historical
run's Content Opportunities tab to stop carrying old fixed P1=min/P0=max rows.
"""
from __future__ import annotations

import argparse
from copy import deepcopy

from sqlalchemy import delete

from app import models
from app.database import SessionLocal
from app.services.content_analysis import build_content_opportunity_rows
from app.services.opportunities import build_opportunities


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild Page Analysis derived opportunity outputs without recrawling."
    )
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        run = db.get(models.Run, args.run_id)
        if not run:
            raise SystemExit(f"Run #{args.run_id} not found")

        targets = (
            db.query(models.Target)
            .filter_by(project_id=run.project_id)
            .order_by(models.Target.id)
            .all()
        )
        keywords = (
            db.query(models.Keyword)
            .filter_by(project_id=run.project_id)
            .order_by(models.Keyword.id)
            .all()
        )
        pages = (
            db.query(models.PageResult)
            .filter_by(run_id=run.id)
            .order_by(models.PageResult.id)
            .all()
        )
        densities = (
            db.query(models.DensityResult)
            .join(models.PageResult)
            .filter(models.PageResult.run_id == run.id)
            .all()
        )

        if not pages:
            raise SystemExit("No persisted PageResult rows found; refusing to rebuild.")
        if not densities:
            raise SystemExit("No persisted DensityResult rows found; refusing to rebuild.")

        target_map = {t.id: t for t in targets}
        successful = [p for p in pages if not p.error and int(p.word_count or 0) > 0]
        client_pages = [
            p for p in successful
            if target_map.get(p.target_id)
            and str(target_map[p.target_id].role or "").upper() == "CLIENT"
        ]
        if not client_pages:
            raise SystemExit("No successful persisted CLIENT page found; refusing to rebuild.")

        client_page = client_pages[0]
        competitor_pages = [
            p for p in successful
            if target_map.get(p.target_id)
            and str(target_map[p.target_id].role or "").upper() != "CLIENT"
        ]
        dmap = {(d.page_result_id, d.keyword_id): d for d in densities}
        role_by_page_id = {
            p.id: str(target_map[p.target_id].role or "OTHER").upper()
            for p in pages
            if p.target_id in target_map
        }

        deterministic, _ = build_opportunities(
            client_page,
            competitor_pages,
            keywords,
            dmap,
            role_by_page_id,
        )

        previous_count = db.query(models.Opportunity).filter_by(run_id=run.id).count()
        db.execute(delete(models.Opportunity).where(models.Opportunity.run_id == run.id))
        db.flush()

        for idx, item in enumerate(deterministic, start=1):
            item = dict(item)
            item["opportunity_key"] = f"DET-{idx:03d}"
            db.add(
                models.Opportunity(
                    run_id=run.id,
                    category=item["category"],
                    priority=item["priority"],
                    confidence=item["confidence"],
                    title=item["title"],
                    recommendation=item["recommendation"],
                    evidence_json=item.get("evidence_ids") or [],
                    source="deterministic",
                )
            )
        db.flush()

        new_rows = (
            db.query(models.Opportunity)
            .filter_by(run_id=run.id)
            .order_by(models.Opportunity.id)
            .all()
        )

        analysis = deepcopy(run.analysis_json or {})
        section_rows = analysis.get("section_comparison") or []
        keyword_rows = analysis.get("keyword_placement") or []
        faq_rows = analysis.get("faq_comparison") or []

        analysis["content_opportunities"] = build_content_opportunity_rows(
            section_rows,
            keyword_rows,
            faq_rows,
            db_opportunities=new_rows,
        )
        analysis.setdefault("benchmark_metadata", {})
        analysis["benchmark_metadata"].update(
            {
                "range_assignment": "LOWER_OF_P0_P1_TO_MIN_HIGHER_TO_MAX",
                "range_note": (
                    "P0/P1 remain benchmark URL roles. Per keyword, the lower anchor density "
                    "is Target Min and the higher anchor density is Target Max."
                ),
            }
        )
        run.analysis_json = analysis
        db.commit()

        print(f"Run #{run.id}")
        print(f"Persisted pages            : {len(pages):,}")
        print(f"Persisted density rows     : {len(densities):,}")
        print(f"Old opportunity rows       : {previous_count:,}")
        print(f"New deterministic rows     : {len(new_rows):,}")
        print(
            "Content opportunity rows  :",
            f"{len(analysis.get('content_opportunities') or []):,}",
        )
        print("No crawl/fetch/browser step was executed.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
