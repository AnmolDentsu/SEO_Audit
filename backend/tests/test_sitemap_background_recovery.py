from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import worker
from app.services import sitemap_job_runner, sitemap_recovery


def test_main_run_builds_automatic_background_recovery_metadata(monkeypatch):
    monkeypatch.setattr(
        sitemap_job_runner.settings,
        "sitemap_background_recovery_initial_delay_seconds",
        5.0,
    )
    run = SimpleNamespace(status="completed_with_warnings", phase="complete_with_warnings")
    meta = sitemap_job_runner._background_recovery_metadata(run, 123)

    assert meta["automatic"] is True
    assert meta["state"] == "queued"
    assert meta["recoverable_rows"] == 123
    assert meta["base_status"] == "completed_with_warnings"
    assert meta["next_attempt_at"]


def test_background_recovery_due_observes_not_before():
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    future_row = SimpleNamespace(config_json={
        "_background_recovery": {
            "automatic": True,
            "state": "cooling",
            "next_attempt_at": future,
        }
    })
    due_row = SimpleNamespace(config_json={
        "_background_recovery": {
            "automatic": True,
            "state": "cooling",
            "next_attempt_at": past,
        }
    })

    assert worker._background_recovery_due(future_row) is False
    assert worker._background_recovery_due(due_row) is True


def test_pending_resolution_is_not_a_recovery_candidate():
    row = SimpleNamespace(
        error="Pending page resolution",
        http_status=None,
        classification_evidence_json={"terminal_state": "pending_resolution"},
    )
    assert sitemap_recovery._is_recoverable(row) is False


def test_existing_terminal_run_is_bootstrapped_without_new_crawl(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models
    from app.database import Base

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        project = models.Project(name="Existing Run")
        db.add(project)
        db.flush()
        source = models.SitemapSource(
            project_id=project.id,
            root_url="https://example.com/sitemap.xml",
            role="CLIENT",
            label="Client",
            enabled=True,
        )
        db.add(source)
        db.flush()
        run = models.SitemapRun(
            project_id=project.id,
            run_number=15,
            status="completed_with_warnings",
            phase="complete_with_warnings",
            config_json={},
            analysis_json={},
        )
        db.add(run)
        db.flush()
        db.add(models.SitemapUrl(
            sitemap_run_id=run.id,
            source_id=source.id,
            root_sitemap=source.root_url,
            child_sitemap=source.root_url,
            original_url="https://example.com/blocked",
            normalized_url="https://example.com/blocked",
            final_url="https://example.com/blocked",
            http_status=403,
            fetch_method="curl-cffi",
            retry_count=0,
            broad_page_type="OTHER",
            detailed_page_type="OTHER",
            type_confidence=0,
            classification_method="fetch-error",
            classification_evidence_json={"terminal_state": "fetch_failed", "retryable": True},
            category="Other / Unclassified",
            topic="Other / Unclassified",
            topic_confidence=0,
            topic_method="not-classified",
            h1_json=[], h2_json=[], schema_types_json=[], breadcrumbs_json=[],
            word_count=0, author_present=False, cta_json=[], error="HTTP 403",
        ))
        db.commit()
        run_id = run.id
    finally:
        db.close()

    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker.settings, "sitemap_background_recovery_enabled", True)

    claimed = worker.claim_sitemap_recovery(run_id)
    assert claimed == run_id

    verify = Session()
    try:
        row = verify.get(models.SitemapRun, run_id)
        meta = (row.config_json or {}).get("_background_recovery") or {}
        assert meta["bootstrapped_existing_run"] is True
        assert meta["state"] == "claimed"
        assert meta["recoverable_rows"] == 1
        assert row.status == "completed_with_warnings"
    finally:
        verify.close()
