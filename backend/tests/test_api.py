import os

os.environ["DATABASE_URL"] = "sqlite:///./test_api.db"
os.environ["JOB_EXECUTION_MODE"] = "worker"

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

client = TestClient(app)


def test_project_flow():
    # ------------------------------------------------------------
    # Create project
    # ------------------------------------------------------------
    r = client.post(
        "/api/projects",
        json={
            "name": "Test project",
            "client_name": "Client",
        },
    )

    assert r.status_code == 201

    pid = r.json()["id"]

    # ------------------------------------------------------------
    # Add public client URL
    # ------------------------------------------------------------
    r = client.post(
        f"/api/projects/{pid}/targets",
        json={
            "url": "https://example.com/",
            "role": "CLIENT",
        },
    )

    assert r.status_code == 201

    # ------------------------------------------------------------
    # Add keyword
    # ------------------------------------------------------------
    r = client.post(
        f"/api/projects/{pid}/keywords",
        json={
            "keyword": "example domain",
            "priority": "P0",
            "target_min": 0.1,
            "target_max": 2.0,
        },
    )

    assert r.status_code == 201

    # ------------------------------------------------------------
    # Compliance guard:
    # run must NOT start without explicit public-data confirmation
    # ------------------------------------------------------------
    r = client.post(
        f"/api/projects/{pid}/runs",
        json={},
    )

    assert r.status_code == 422

    # ------------------------------------------------------------
    # Valid run request with compliance acknowledgement
    # ------------------------------------------------------------
    r = client.post(
        f"/api/projects/{pid}/runs",
        json={
            "public_data_confirmed": True,
            "use_cache": True,
            "force_refresh": False,
            "enable_ai": True,
        },
    )

    assert r.status_code == 201

    body = r.json()

    assert body["status"] == "queued"
    assert body["project_id"] == pid


def test_run_rejects_missing_public_data_confirmation():
    # ------------------------------------------------------------
    # Dedicated compliance regression test.
    #
    # If somebody later accidentally removes the safeguard,
    # this test must fail.
    # ------------------------------------------------------------
    r = client.post(
        "/api/projects",
        json={
            "name": "Compliance test project",
            "client_name": "Client",
        },
    )

    assert r.status_code == 201

    pid = r.json()["id"]

    r = client.post(
        f"/api/projects/{pid}/targets",
        json={
            "url": "https://example.com/",
            "role": "CLIENT",
        },
    )

    assert r.status_code == 201

    r = client.post(
        f"/api/projects/{pid}/keywords",
        json={
            "keyword": "public seo keyword",
            "priority": "P0",
            "target_min": 0.1,
            "target_max": 2.0,
        },
    )

    assert r.status_code == 201

    r = client.post(
        f"/api/projects/{pid}/runs",
        json={
            "public_data_confirmed": False,
        },
    )

    assert r.status_code == 422