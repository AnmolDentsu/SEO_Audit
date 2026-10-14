from app.services.google_auth import auth_summary


def test_auth_summary_does_not_return_secret():
    data = auth_summary().safe_dict()
    assert "gemini_api_key" not in data
    assert set(data) >= {"ai_mode", "auth_source", "project", "model", "embedding_model"}
