import httpx
from app.config import get_settings

settings = get_settings()


def dispatch_cloud_run_job(run_id: int, run_kind: str = "page") -> str:
    project = settings.cloud_run_project or settings.google_cloud_project
    region = settings.cloud_run_region
    job = settings.cloud_run_job_name
    if not project or not job:
        raise RuntimeError("CLOUD_RUN_PROJECT/GOOGLE_CLOUD_PROJECT and CLOUD_RUN_JOB_NAME are required")
    import google.auth
    from google.auth.transport.requests import Request
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    url = f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run"
    args = ["-m", "app.worker", "--once"]
    if run_kind == "sitemap":
        args += ["--sitemap-run-id", str(run_id)]
    else:
        args += ["--run-id", str(run_id)]
    body = {"overrides": {"containerOverrides": [{"args": args}]}}
    response = httpx.post(url, headers={"Authorization": f"Bearer {credentials.token}"}, json=body, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("name", "submitted")
