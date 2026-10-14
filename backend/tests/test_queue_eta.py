from datetime import (
    datetime,
    timedelta,
    timezone,
)

from sqlalchemy import (
    create_engine,
)

from sqlalchemy.orm import (
    sessionmaker,
)

from app import models

from app.database import Base

from app.services.queue_eta import (
    queue_status,
)


def make_db():
    engine = create_engine(
        "sqlite:///:memory:"
    )

    Base.metadata.create_all(
        engine
    )

    Session = sessionmaker(
        bind=engine
    )

    return Session()


def test_page_queue_waits_for_active_worker(
    monkeypatch,
):
    monkeypatch.setenv(
        "PAGE_WORKER_CAPACITY",
        "1",
    )

    db = make_db()

    now = datetime.now(
        timezone.utc
    )

    project = models.Project(
        name="Queue Test"
    )

    db.add(project)
    db.flush()

    running = models.Run(
        project_id=project.id,
        run_number=1,
        status="running",
        phase="crawl",
        progress_current=1,
        progress_total=2,
        created_at=(
            now
            - timedelta(
                minutes=10
            )
        ),
        started_at=(
            now
            - timedelta(
                minutes=10
            )
        ),
        config_json={
            "_progress": {
                "elapsed_seconds": 600,
                "overall_percent": 50,
            },
            "_input_snapshot": {
                "targets": [
                    {"id": 1},
                    {"id": 2},
                ],
            },
        },
        analysis_json={},
    )

    queued = models.Run(
        project_id=project.id,
        run_number=2,
        status="queued",
        progress_total=2,
        created_at=now,
        config_json={
            "_input_snapshot": {
                "targets": [
                    {"id": 3},
                    {"id": 4},
                ],
            },
        },
        analysis_json={},
    )

    db.add_all(
        [
            running,
            queued,
        ]
    )

    db.commit()

    result = queue_status(
        db,
        "page",
        queued.id,
    )

    assert (
        result["queue_position"]
        == 1
    )

    assert (
        result["jobs_ahead"]
        >= 1
    )

    assert (
        result[
            "estimated_start_seconds"
        ]["high"]
        > 0
    )

    db.close()


def test_page_and_sitemap_queues_are_independent(
    monkeypatch,
):
    monkeypatch.setenv(
        "PAGE_WORKER_CAPACITY",
        "1",
    )

    monkeypatch.setenv(
        "SITEMAP_WORKER_CAPACITY",
        "1",
    )

    db = make_db()

    project = models.Project(
        name="Independent Queues"
    )

    db.add(project)
    db.flush()

    page = models.Run(
        project_id=project.id,
        run_number=1,
        status="running",
        progress_total=1,
        config_json={},
        analysis_json={},
    )

    sitemap = models.SitemapRun(
        project_id=project.id,
        run_number=1,
        status="queued",
        progress_total=1000,
        config_json={
            "_input_snapshot": {
                "sources": [
                    {
                        "root_url": (
                            "https://example.com/"
                            "sitemap.xml"
                        )
                    }
                ]
            }
        },
        analysis_json={},
    )

    db.add_all(
        [
            page,
            sitemap,
        ]
    )

    db.commit()

    result = queue_status(
        db,
        "sitemap",
        sitemap.id,
    )

    # Running page jobs do not occupy
    # Sitemap worker lanes.
    assert (
        result["active_count"]
        == 0
    )

    assert (
        result[
            "estimated_start_seconds"
        ]["low"]
        == 0
    )

    db.close()