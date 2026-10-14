"""Rebuild Sitemap Intelligence taxonomy/coverage/opportunities from persisted URLs.

Usage from backend directory:
    source .venv/bin/activate
    python scripts/rebuild_sitemap_analysis.py --run-id 6
    python scripts/rebuild_sitemap_analysis.py --run-id 6 --enable-ai

This does NOT recrawl sitemap/page URLs. It reuses SitemapUrl rows already stored for the run.
"""
from __future__ import annotations

import argparse
from app import models
from app.database import SessionLocal
from app.services.sitemap_analysis import build_sitemap_analysis
from app.services.sitemap_ai import review_sitemap_opportunities_ai
from app.services.sitemap_topics import build_dynamic_topic_taxonomy


def _taxonomy_payload(row: models.SitemapUrl) -> dict:
    return {
        "item_id": row.id,
        "requested_url": row.original_url,
        "final_url": row.final_url or row.original_url,
        "title": row.title or "",
        "meta_description": row.meta_description or "",
        "h1": row.h1_json or [],
        "h2": row.h2_json or [],
        "breadcrumbs": row.breadcrumbs_json or [],
        "schema_types": row.schema_types_json or [],
        "detailed_page_type": row.detailed_page_type or "OTHER",
        "classification_evidence": row.classification_evidence_json or {},
        "error": row.error,
        "category": row.category,
        "topic": row.topic,
        "subtopic": row.subtopic,
        "topic_confidence": row.topic_confidence,
        "topic_method": row.topic_method,
    }


def rebuild(run_id: int, enable_ai: bool = False) -> None:
    db = SessionLocal()
    try:
        run = db.get(models.SitemapRun, run_id)
        if not run:
            raise SystemExit(f"Sitemap run {run_id} not found")
        sources = db.query(models.SitemapSource).filter_by(project_id=run.project_id).order_by(models.SitemapSource.id).all()
        rows = db.query(models.SitemapUrl).filter_by(sitemap_run_id=run_id).order_by(models.SitemapUrl.id).all()
        if not rows:
            raise SystemExit(f"Sitemap run {run_id} has no persisted SitemapUrl rows")

        eligible_rows = []
        row_by_id = {x.id: x for x in rows}
        for row in rows:
            evidence = row.classification_evidence_json or {}
            if row.error or evidence.get("taxonomy_eligible") is False:
                continue
            eligible_rows.append(_taxonomy_payload(row))

        print(f"Rebuilding taxonomy from {len(eligible_rows):,} eligible rows (no crawl)...")
        clusters = build_dynamic_topic_taxonomy(db, eligible_rows, enable_ai=enable_ai)

        updated = 0
        for payload in eligible_rows:
            row = row_by_id.get(int(payload["item_id"]))
            if not row:
                continue
            row.category = payload.get("category") or row.category
            row.topic = payload.get("topic") or row.topic
            row.subtopic = payload.get("subtopic") or row.subtopic
            row.topic_confidence = payload.get("topic_confidence") if payload.get("topic_confidence") is not None else row.topic_confidence
            row.topic_method = payload.get("topic_method") or row.topic_method
            updated += 1
            if updated % 5000 == 0:
                db.commit()
                print(f"Updated {updated:,} taxonomy rows...")
        db.commit()

        # Re-query so analysis sees the freshly persisted taxonomy.
        rows = db.query(models.SitemapUrl).filter_by(sitemap_run_id=run_id).order_by(models.SitemapUrl.id).all()
        old_analysis = run.analysis_json or {}
        source_discovery = old_analysis.get("source_discovery") or old_analysis.get("discovery_scope") or []
        analysis, opportunities, evidence_index = build_sitemap_analysis(rows, sources, source_discovery=source_discovery)
        analysis["dynamic_topic_clusters"] = clusters

        reviews = {}
        if enable_ai:
            candidates = [x for x in opportunities if str(x.get("priority") or "").upper() in {"HIGH", "MEDIUM"}]
            print(f"AI reviewing up to configured limit from {len(candidates):,} candidates...")
            reviews = review_sitemap_opportunities_ai(candidates, evidence_index)

        final = []
        for opp in opportunities:
            review = reviews.get(opp["opportunity_key"])
            if review:
                decision = str(review.get("decision") or "REVIEW").upper()
                opp["ai_decision"] = decision
                opp["ai_review_confidence"] = float(review.get("confidence") or 0)
                opp["business_relevance"] = review.get("business_relevance") or opp.get("business_relevance")
                if review.get("recommended_page_type") not in {None, "", "NO_CHANGE"}:
                    opp["recommended_page_type"] = review["recommended_page_type"]
                if review.get("recommendation"):
                    opp["recommendation"] = review["recommendation"]
                opp["evidence_ids"] = list(dict.fromkeys((opp.get("evidence_ids") or []) + (review.get("evidence_ids") or [])))
                opp["source"] = "deterministic+ai-reviewed"
                if decision == "NO_CHANGE":
                    continue
            final.append(opp)

        db.query(models.SitemapOpportunity).filter_by(sitemap_run_id=run_id).delete(synchronize_session=False)
        for opp in final:
            db.add(models.SitemapOpportunity(
                sitemap_run_id=run_id,
                opportunity_key=opp["opportunity_key"],
                gap_type=opp["gap_type"],
                priority=opp["priority"],
                confidence=float(opp.get("confidence") or 0),
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
        analysis["content_opportunities"] = final
        run.analysis_json = analysis
        # Keep original terminal status/time; this is analysis repair, not a recrawl.
        db.commit()
        print(f"Done. Clusters={len(clusters):,}; opportunities={len(final):,}; taxonomy rows updated={updated:,}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--enable-ai", action="store_true", help="Run bounded AI cluster labeling/opportunity review; still no recrawl")
    args = parser.parse_args()
    rebuild(args.run_id, enable_ai=args.enable_ai)
