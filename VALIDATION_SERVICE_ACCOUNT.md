# Service-account Gemini integration validation

Updated on 2026-08-21.

Validated locally in the build environment:

- Python source compilation succeeds for backend application, tests, and `scripts/check_gemini_auth.py`.
- `.env` alias resolution succeeds for `GCP_PROJECT_ID`, `GCP_LOCATION`, `MODEL_TEXT`, and `MODEL_EMBEDDING`.
- The project now has a single Google Gen AI client factory used by both generation and embeddings.
- Safe AI status/test API routes are wired into FastAPI.
- Local Docker service-account override is included.
- Credential filenames are gitignored and API responses do not expose secret values.

Not live-validated in this sandbox:

- A real Vertex AI request, because the sandbox has no user GCP credentials and cannot install Google packages from the public package registry during this session.
- Your specific model access/quota. Run `python scripts/check_gemini_auth.py` locally after configuring `.env` to verify this.
