from __future__ import annotations

import argparse
import hashlib
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from sqlalchemy import delete

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.sitemap_ai import review_sitemap_opportunities_ai
from app.services.sitemap_analysis import build_sitemap_analysis
from app.services.sitemap_result_views import compact_analysis_payload, result_quality

settings = get_settings()


def _now():
    return datetime.now(timezone.utc)


def _set_progress(
    db,
    run,
    *,
    status: str,
    phase: str,
    percent: float,
    message: str,
    stage_fraction: float | None = None,
    current: int | None = None,
    total: int | None = None,
):
    """Persist recovery progress with a real heartbeat and stage counters."""
    now = _now()
    cfg = dict(run.config_json or {})
    old = dict(cfg.get("_progress") or {})

    if old.get("stage_key") != phase:
        stage_started_at = now.isoformat()
    else:
        stage_started_at = old.get("stage_started_at") or now.isoformat()

    if stage_fraction is None:
        if percent >= 100:
            stage_percent = 100.0
        else:
            stage_percent = float(old.get("stage_percent") or 0.0)
    else:
        stage_percent = round(
            max(0.0, min(1.0, float(stage_fraction))) * 100,
            2,
        )

    old.update({
        "percent": round(percent, 1),
        "overall_percent": round(percent, 1),
        "stage_percent": stage_percent,
        "stage_key": phase,
        "stage_label": phase.replace("_", " ").title(),
        "message": message,
        "stage_current": current,
        "stage_total": total,
        "stage_started_at": stage_started_at,
        "heartbeat_at": now.isoformat(),
    })
    cfg["_progress"] = old
    run.config_json = cfg
    run.status = status
    run.phase = phase
    run.progress_current = int(round(percent / 100 * 1000))
    run.progress_total = 1000
    db.commit()


def _taxonomy_eligible(row) -> bool:
    if row.error:
        return False
    evidence = row.classification_evidence_json or {}
    if isinstance(evidence, dict):
        if str(evidence.get("terminal_state") or "usable").lower() != "usable":
            return False
        if evidence.get("taxonomy_eligible") is False:
            return False
    return str(row.detailed_page_type or "OTHER").upper() in {
        "PRODUCT", "CONTENT", "BLOG", "CATEGORY", "TOOL_CALCULATOR",
        "FAQ_HELP", "LANDING_PAGE",
    }


def _reconstruct_cluster_summary(rows, limit: int = 5000):
    counts = Counter()
    confidence_sum = defaultdict(float)
    methods = defaultdict(Counter)
    variants = defaultdict(set)

    for row in rows:
        if not _taxonomy_eligible(row):
            continue
        category = row.category or "Other / Unclassified"
        topic = row.topic or "Other / Unclassified"
        if category == "Other / Unclassified" or topic == "Other / Unclassified":
            continue
        key = (category, topic)
        counts[key] += 1
        confidence_sum[key] += float(row.topic_confidence or 0)
        methods[key][str(row.topic_method or "unknown")] += 1
        if row.subtopic and len(variants[key]) < 10:
            variants[key].add(str(row.subtopic).strip())

    total = len(counts)
    selected = counts.most_common(None if limit <= 0 else limit)
    output = []
    for (category, topic), page_count in selected:
        raw = f"{category}\x1f{topic}".encode("utf-8", "ignore")
        output.append({
            "cluster_id": "REC-" + hashlib.sha1(raw).hexdigest()[:12],
            "category": category,
            "topic": topic,
            "confidence": round(confidence_sum[(category, topic)] / max(page_count, 1), 3),
            "method": methods[(category, topic)].most_common(1)[0][0] if methods[(category, topic)] else "recovered",
            "page_count": page_count,
            "variants": " | ".join(sorted(x for x in variants[(category, topic)] if x)[:10]),
        })
    return output, total


def main():
    parser = argparse.ArgumentParser(
        description="Resume a Sitemap Intelligence run directly from already-persisted SitemapUrl rows."
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--skip-ai", action="store_true", help="Skip final AI opportunity review for fastest completion")
    parser.add_argument(
        "--cluster-summary-limit", type=int, default=5000,
        help="Maximum reconstructed dynamic-cluster rows stored in analysis_json; 0 = unlimited",
    )
    args = parser.parse_args()

    db = SessionLocal()
    run_id = args.run_id
    started = time.perf_counter()

    try:
        run = db.get(models.SitemapRun, run_id)
        if not run:
            raise SystemExit(f"Sitemap run #{run_id} not found")

        sources = (
            db.query(models.SitemapSource)
            .filter_by(project_id=run.project_id, enabled=True)
            .order_by(models.SitemapSource.id)
            .all()
        )
        if not sources:
            raise SystemExit("No enabled sitemap sources found for this project")

        url_count = (
            db.query(models.SitemapUrl)
            .filter_by(sitemap_run_id=run_id)
            .count()
        )
        if url_count <= 0:
            raise SystemExit(
                "No persisted SitemapUrl rows exist for this run. Coverage-only recovery is unsafe; resume the normal worker instead."
            )

        cfg = dict(run.config_json or {})
        source_discovery = list(cfg.get("_sitemap_discovery") or [])
        if not source_discovery:
            source_discovery = list((run.analysis_json or {}).get("source_discovery") or [])

        print(f"Run #{run_id}")
        print(f"Persisted sitemap URL rows: {url_count:,}")
        print(f"Sources: {len(sources)}")
        print("Skipping XML discovery, HTTP resolution, browser fallback and topic reclassification.")

        _set_progress(
            db, run,
            status="running",
            phase="coverage_analysis",
            percent=84.0,
            message=f"Coverage recovery from {url_count:,} persisted sitemap rows",
            stage_fraction=0.0,
            current=0,
            total=url_count,
        )

        t0 = time.perf_counter()
        rows = (
            db.query(models.SitemapUrl)
            .filter_by(sitemap_run_id=run_id)
            .order_by(models.SitemapUrl.id)
            .all()
        )
        print(f"Loaded persisted rows in {time.perf_counter() - t0:.1f}s")

        last_progress_print = 0.0

        def coverage_progress(fraction, message, current=None, total=None):
            nonlocal last_progress_print
            fraction = max(0.0, min(1.0, float(fraction)))
            percent = 84.0 + 8.0 * fraction

            _set_progress(
                db,
                run,
                status="running",
                phase="coverage_analysis",
                percent=percent,
                message=message,
                stage_fraction=fraction,
                current=current,
                total=total,
            )

            # Keep terminal output useful without printing on every small index step.
            now_mono = time.perf_counter()
            if (
                fraction >= 1.0
                or now_mono - last_progress_print >= 5.0
            ):
                if current is not None and total is not None:
                    print(
                        f"Coverage {fraction * 100:6.2f}% | "
                        f"{current:,}/{total:,} | {message}"
                    )
                else:
                    print(f"Coverage {fraction * 100:6.2f}% | {message}")
                last_progress_print = now_mono

        t0 = time.perf_counter()
        analysis, opportunities, evidence_index = build_sitemap_analysis(
            rows,
            sources,
            source_discovery=source_discovery,
            progress_callback=coverage_progress,
        )
        print(
            "Coverage built in "
            f"{time.perf_counter() - t0:.1f}s; "
            f"topics={len(analysis.get('topic_coverage_matrix') or []):,}, "
            f"opportunities={len(opportunities):,}"
        )

        # The original runner held a huge in-memory taxonomy-cluster list that is
        # lost when a process is interrupted after page persistence. Reconstruct a
        # bounded audit summary from the already-persisted per-page taxonomy.
        t0 = time.perf_counter()
        clusters, cluster_total = _reconstruct_cluster_summary(
            rows,
            limit=max(0, int(args.cluster_summary_limit)),
        )
        analysis["dynamic_topic_clusters"] = clusters
        analysis["dynamic_topic_clusters_total"] = cluster_total
        analysis["dynamic_topic_clusters_truncated"] = (
            args.cluster_summary_limit > 0 and cluster_total > args.cluster_summary_limit
        )
        analysis["coverage_recovery"] = {
            "recovered_from_persisted_urls": True,
            "persisted_url_rows": url_count,
            "cluster_summary_rows": len(clusters),
            "cluster_total": cluster_total,
            "reason": "Resumed after coverage-stage interruption without repeating crawl/resolution/topic classification.",
        }
        print(
            f"Cluster audit summary rebuilt in {time.perf_counter() - t0:.1f}s "
            f"({len(clusters):,}/{cluster_total:,} rows stored in analysis JSON)"
        )

        comparison_ready = bool((analysis.get("comparison_status") or {}).get("ready"))
        ai_reviews = {}

        if (
            not args.skip_ai
            and comparison_ready
            and settings.ai_mode.strip().lower() not in {"off", "mock"}
        ):
            ai_candidates = [
                x
                for x in opportunities
                if str(x.get("priority") or "").upper() in {"HIGH", "MEDIUM"}
            ][: settings.sitemap_ai_max_opportunities]

            if ai_candidates:
                _set_progress(
                    db,
                    run,
                    status="running",
                    phase="ai_opportunity_review",
                    percent=94.0,
                    message=(
                        f"AI review of up to {len(ai_candidates):,} "
                        "high/medium opportunities"
                    ),
                    stage_fraction=0.0,
                    current=0,
                    total=len(ai_candidates),
                )
                t0 = time.perf_counter()
                ai_reviews = review_sitemap_opportunities_ai(
                    ai_candidates,
                    evidence_index,
                )
                print(
                    "AI opportunity review finished in "
                    f"{time.perf_counter() - t0:.1f}s"
                )
                _set_progress(
                    db,
                    run,
                    status="running",
                    phase="ai_opportunity_review",
                    percent=98.0,
                    message="AI-model opportunity review complete",
                    stage_fraction=1.0,
                    current=len(ai_candidates),
                    total=len(ai_candidates),
                )

        # Keep deterministic facts authoritative. AI may review relevance,
        # wording and recommended action, but it must not overwrite measured
        # counts, deterministic priority score, or deterministic evidence strength.
        final_opportunities = []
        for opp in opportunities:
            review = ai_reviews.get(opp["opportunity_key"])
            if review:
                decision = str(review.get("decision") or "REVIEW").upper()
                opp["ai_decision"] = decision
                opp["business_relevance"] = (
                    review.get("business_relevance")
                    or opp["business_relevance"]
                )
                opp["ai_review_confidence"] = float(
                    review.get("confidence") or 0
                )

                recommended = review.get("recommended_page_type")
                if recommended and recommended != "NO_CHANGE":
                    opp["recommended_page_type"] = recommended

                if review.get("recommendation"):
                    opp["recommendation"] = review["recommendation"]

                opp["evidence_ids"] = list(dict.fromkeys(
                    (opp.get("evidence_ids") or [])
                    + (review.get("evidence_ids") or [])
                ))
                opp["source"] = "deterministic+ai-reviewed"
                opp["supporting_subtopics"] = (
                    review.get("supporting_subtopics") or []
                )

                if decision == "NO_CHANGE":
                    continue

            final_opportunities.append(opp)

        # Idempotent recovery: replace only this run's derived opportunity rows.
        db.execute(
            delete(models.SitemapOpportunity)
            .where(models.SitemapOpportunity.sitemap_run_id == run_id)
        )
        db.commit()

        db_batch = []
        persisted_opportunities = 0
        total_final = len(final_opportunities)

        for index, opp in enumerate(final_opportunities, start=1):
            db_batch.append(models.SitemapOpportunity(
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
                business_relevance=(
                    opp.get("business_relevance") or "uncertain"
                ),
                title=opp["title"],
                recommendation=opp["recommendation"],
                evidence_json=opp.get("evidence_ids") or [],
                source=opp.get("source") or "deterministic",
            ))

            if len(db_batch) >= 1000:
                db.add_all(db_batch)
                db.commit()
                persisted_opportunities += len(db_batch)
                db_batch.clear()

                _set_progress(
                    db,
                    run,
                    status="running",
                    phase="persisting_opportunities",
                    percent=99.0,
                    message=(
                        "Persisting final opportunities "
                        f"({persisted_opportunities:,}/{total_final:,})"
                    ),
                    stage_fraction=(
                        persisted_opportunities / max(total_final, 1)
                    ),
                    current=persisted_opportunities,
                    total=total_final,
                )

        if db_batch:
            db.add_all(db_batch)
            db.commit()
            persisted_opportunities += len(db_batch)

        analysis["content_opportunities"] = final_opportunities

        # Do not duplicate huge opportunity/topic matrices into one JSON blob.
        # They are served lazily from SitemapUrl/SitemapOpportunity tables.
        quality = result_quality(
            db,
            run_id,
            opportunity_count=len(final_opportunities),
        )
        compact = compact_analysis_payload(
            analysis,
            quality=quality,
            opportunity_count=len(final_opportunities),
        )
        run = db.get(models.SitemapRun, run_id)
        run.analysis_json = compact
        cfg = dict(run.config_json or {})
        cfg["_result_summary"] = {
            "comparison_status": compact.get("comparison_status") or {},
            "quality": quality,
            "result_counts": compact.get("result_counts") or {},
        }
        run.config_json = cfg

        failed_source_count = sum(1 for x in source_discovery if x.get("status") == "failed")
        partial_source_count = sum(1 for x in source_discovery if x.get("status") == "partial")
        comparison_ready = bool((compact.get("comparison_status") or {}).get("ready"))

        if failed_source_count or partial_source_count or not comparison_ready:
            run.status = "partial"
            run.phase = "partial"
            run.error = (
                f"Partial sitemap analysis: {failed_source_count} source(s) failed, "
                f"{partial_source_count} source(s) partially discovered. "
                f"{(compact.get('comparison_status') or {}).get('message', '')}"
            ).strip()
            final_status = "partial"
            final_message = "Coverage recovery complete with incomplete source coverage"
        elif quality.get("status") == "warning":
            run.status = "completed_with_warnings"
            run.phase = "quality_warning"
            run.error = " ".join(quality.get("warnings") or [])
            final_status = "completed_with_warnings"
            final_message = "Coverage recovery complete; taxonomy/output quality review required"
        else:
            run.status = "completed"
            run.phase = "complete"
            run.error = None
            final_status = "completed"
            final_message = "Sitemap Intelligence analysis complete (coverage-stage recovery)"

        run.completed_at = _now()
        db.commit()
        _set_progress(
            db, run,
            status=final_status,
            phase=run.phase,
            percent=100.0,
            message=final_message,
        )

        print("\nDONE")
        print("status:", run.status)
        print("phase:", run.phase)
        print("persisted URL rows:", f"{url_count:,}")
        print("opportunities:", f"{len(final_opportunities):,}")
        print("elapsed:", f"{time.perf_counter() - started:.1f}s")

    except Exception:
        db.rollback()
        try:
            run = db.get(models.SitemapRun, run_id)
            if run:
                run.status = "failed"
                run.phase = "coverage_recovery_failed"
                run.error = "Coverage-stage recovery failed. See terminal traceback."
                db.commit()
        finally:
            db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
