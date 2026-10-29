import pytest

from fastapi.testclient import TestClient

from app.auth_config import get_auth_settings
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def test_auth_me_local_development_mode(
    client,
):
    settings = get_auth_settings()

    if settings.auth_enabled:
        pytest.skip(
            "Local-development auth test requires AUTH_ENABLED=false"
        )

    response = client.get(
        "/auth/me"
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["authenticated"] is True
    assert payload["user"]["sub"] == "local-development"


def test_local_login_redirects_to_site(
    client,
):
    settings = get_auth_settings()

    if settings.auth_enabled:
        pytest.skip(
            "Local-development auth test requires AUTH_ENABLED=false"
        )

    response = client.get(
        "/auth/login",
        follow_redirects=False,
    )

    assert response.status_code in {
        302,
        307,
    }

    assert response.headers[
        "location"
    ] == settings.normalized_site_url