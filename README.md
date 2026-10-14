# SEO AI Platform — React + FastAPI

A complete runnable SEO analysis platform with:

- **React + Vite frontend**
- **FastAPI backend**
- **Playwright crawler** with retry/backoff, cache, checkpointing and SSRF protections
- **Phase 1** exact keyword/phrase density analysis for Client, P0, P1 and other competitor URLs
- **Phase 2** title/meta/heading/content-structure comparison and evidence-backed opportunities
- Optional **Vertex AI Gemini** structured reasoning using **service-account credentials or Application Default Credentials (ADC)**
- Optional **Gemini Developer API key mode** for local fallback/testing; Vertex service-account mode remains preferred
- Optional Vertex **Gemini embeddings** for semantic heading-gap comparison
- SQLite for local development; PostgreSQL/Cloud SQL recommended for production
- Separate local worker and optional **Cloud Run Job** execution mode for long-running crawl jobs
- Excel export with summary, density, structure, opportunities, failures and evidence sheets

## 1. Prerequisites

Install:

- Python **3.11–3.13**
- Node.js **20+**
- Git
- Google Cloud CLI only if you want Vertex AI or GCP deployment
- Docker Desktop only if you want the container path

Check:

```bash
python3 --version
node --version
npm --version
git --version
```

## 2. Extract / enter the project

```bash
cd seo-ai-platform
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

## 3. Fastest automated local setup

macOS/Linux:

```bash
chmod +x scripts/setup_local.sh
./scripts/setup_local.sh
```

Windows PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_windows.ps1
```

Or follow the manual steps below.

## 4. Backend setup

### macOS / Linux

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
playwright install chromium
cd ..
```

If Chromium system libraries are missing on Linux:

```bash
cd backend
source .venv/bin/activate
playwright install --with-deps chromium
cd ..
```

### Windows PowerShell

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
playwright install chromium
cd ..
```

### Windows Git Bash

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
playwright install chromium
cd ..
```

## 5. Initialize / migrate the database

From `backend/` with the venv active:

```bash
alembic upgrade head
```

Optional demo seed:

```bash
python -m app.cli seed-demo
```

## 6. Frontend setup

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

Windows PowerShell:

```powershell
cd frontend
npm install
Copy-Item .env.example .env
npm run dev
```

Frontend: <http://localhost:5173>

## 7. Run locally — no AI (recommended first test)

Set in root `.env`:

```env
AI_MODE=off
JOB_EXECUTION_MODE=worker
DATABASE_URL=sqlite:///./seo_platform.db
```

Terminal 1 — API:

```bash
cd backend
source .venv/bin/activate   # Windows Git Bash: source .venv/Scripts/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Terminal 2 — worker:

```bash
cd backend
source .venv/bin/activate
python -m app.worker
```

Terminal 3 — frontend:

```bash
cd frontend
npm run dev
```

Open <http://localhost:5173>, create a project, add URLs and keywords, then click **Start analysis**.

### One-process development mode

If you do not want a separate worker during a quick local test:

```env
JOB_EXECUTION_MODE=inline
```

Then only run FastAPI + React. Inline mode starts the job in a FastAPI background task; do **not** use it for production crawls.

## 8. Gemini / Vertex AI authentication and configuration

The backend supports four modes:

```env
AI_MODE=off
AI_MODE=mock
AI_MODE=vertex
AI_MODE=api_key
```

For your existing enterprise-style `.env` setup, use **Vertex AI service-account mode**. In this mode the service account authenticates the Google Gen AI SDK; `GEMINI_API_KEY` is optional and is **not used**. Google documents API-key authentication for the Gemini Developer API separately from Vertex AI credentials, so the project deliberately does not combine the two auth methods in one client.

### 8.1 Recommended local service-account configuration

Enable Vertex AI in the project:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com
```

The service account needs permission to call Vertex AI. A common project-level role for development is:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:YOUR_SERVICE_ACCOUNT_EMAIL" \
  --role="roles/aiplatform.user"
```

Prefer storing the JSON key outside the Git repository. Example:

```text
~/gcp-credentials/seo-platform-service-account.json
```

Then set root `.env`:

```env
AI_MODE=vertex
VERTEX_AUTH_MODE=service_account

GCP_PROJECT_ID=YOUR_PROJECT_ID
GCP_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/Users/YOUR_USER/gcp-credentials/seo-platform-service-account.json

MODEL_TEXT=gemini-3.5-flash
MODEL_EMBEDDING=gemini-embedding-2
EMBEDDING_DIMENSION=768
AI_MAX_OPPORTUNITIES=20

# Optional only. Ignored while AI_MODE=vertex.
GEMINI_API_KEY=
```

If you intentionally want the same layout as your existing project, you may keep `service_account.json` in the repo root for **local-only** testing:

```env
AI_MODE=vertex
VERTEX_AUTH_MODE=service_account
GCP_PROJECT_ID=YOUR_PROJECT_ID
GCP_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=./service_account.json
MODEL_TEXT=YOUR_ENABLED_GEMINI_MODEL
MODEL_EMBEDDING=gemini-embedding-2
```

`service_account*.json` is included in `.gitignore`, but keeping private keys outside the repository is still safer.

### 8.2 Verify service-account/Gemini configuration before running the app

From the project root:

```bash
cd backend
source .venv/bin/activate
cd ..
python scripts/check_gemini_auth.py
```

The script prints only safe metadata, such as:

```text
auth_source = service_account_file
project = YOUR_PROJECT_ID
location = global
model = ...
embedding_model = gemini-embedding-2
```

and performs one minimal Gemini request. It never prints the API key or service-account private key.

Once FastAPI is running you can also inspect:

```bash
curl http://localhost:8000/api/ai/status
curl -X POST http://localhost:8000/api/ai/test
```

### 8.3 ADC mode instead of a downloaded JSON key

For local user ADC:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Then:

```env
AI_MODE=vertex
VERTEX_AUTH_MODE=adc
GCP_PROJECT_ID=YOUR_PROJECT_ID
GCP_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=
MODEL_TEXT=gemini-3.5-flash
MODEL_EMBEDDING=gemini-embedding-2
```

On Cloud Run, prefer this same `VERTEX_AUTH_MODE=adc` configuration with a user-managed service account attached to the service/job. Do **not** deploy the downloaded JSON key to Cloud Run.

### 8.4 Optional Gemini API-key mode

If you explicitly want the Gemini Developer API instead of Vertex AI:

```env
AI_MODE=api_key
GEMINI_API_KEY=YOUR_KEY
MODEL_TEXT=YOUR_GEMINI_DEVELOPER_API_MODEL
MODEL_EMBEDDING=gemini-embedding-2
```

In `AI_MODE=api_key`, `GCP_PROJECT_ID` and `GOOGLE_APPLICATION_CREDENTIALS` are not used to authenticate Gemini.

### 8.5 What the code does

```text
AI_MODE=vertex
       │
       ├─ GOOGLE_APPLICATION_CREDENTIALS present
       │        ↓
       │  Load service-account credentials
       │        ↓
       │  google.genai.Client(vertexai=True, project=..., location=..., credentials=...)
       │
       └─ no credential file / VERTEX_AUTH_MODE=adc
                ↓
          google.auth.default()
                ↓
          gcloud ADC locally OR attached Cloud Run service account

Deterministic SEO engine
       ↓
Embeddings for semantic matching
       ↓
Gemini structured recommendation refinement
       ↓
If Gemini fails → deterministic results are retained
```

The following `.env` aliases are accepted for compatibility with existing projects:

```text
GCP_PROJECT_ID           or GOOGLE_CLOUD_PROJECT
GCP_LOCATION             or GOOGLE_CLOUD_LOCATION
MODEL_TEXT               or GEMINI_MODEL
MODEL_EMBEDDING          or EMBEDDING_MODEL
```

### 8.6 Local Docker Compose with the service-account file

The base Compose file keeps AI disabled. For local Docker testing with `./service_account.json` use the provided override:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.vertex.yml \
  up --build
```

The JSON is mounted read-only inside API and worker containers. This is for local testing only; deployed Google Cloud services should use attached service-account identity.

## 9. API endpoints

- `GET /healthz`
- `GET /readyz`
- `GET /api/ai/status`
- `POST /api/ai/test`
- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/{project_id}`
- `POST /api/projects/{project_id}/targets`
- `POST /api/projects/{project_id}/keywords`
- `DELETE /api/targets/{target_id}`
- `DELETE /api/keywords/{keyword_id}`
- `POST /api/projects/{project_id}/runs`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/phase1`
- `GET /api/runs/{run_id}/phase2`
- `GET /api/runs/{run_id}/opportunities`
- `GET /api/runs/{run_id}/failures`
- `GET /api/runs/{run_id}/evidence/{evidence_id}`
- `GET /api/runs/{run_id}/export.xlsx`

Interactive docs: <http://localhost:8000/docs>

## 10. What Phase 1 calculates

For every URL × keyword/phrase:

- exact normalized phrase count
- total visible main-content words
- token-weighted density % = `(phrase occurrences × words in phrase / total words) × 100`
- present / absent
- configured keyword priority
- target minimum / maximum thresholds

Navigation, footer, cookie banners, login/signup and common utility containers are removed before text extraction. The crawler records timestamp, status, retry count and crawl error details.

## 11. What Phase 2 calculates

For each successfully crawled page:

- title and title length
- meta description and length
- H1/H2/H3 lists
- main-content word count
- keyword presence in title/H1/headings/body
- client-vs-competitor density gaps
- missing/common competitor heading themes
- under-target / over-target keyword usage
- evidence-backed opportunities with `priority`, `confidence`, `source` and evidence IDs

With `AI_MODE=vertex`, Gemini receives **only a compact deterministic evidence payload**, not raw unrestricted browser state, and must respond in a JSON schema.

## 12. CSV keyword import format

The React UI accepts a CSV with these headers:

```csv
keyword,priority,target_min,target_max
home loan,P0,0.4,1.2
interest rate,P1,0.2,0.8
calculator,NONE,,
```

Priority values: `P0`, `P1`, `NONE`.

## 13. Tests

Backend:

```bash
cd backend
source .venv/bin/activate
pytest -q
```

Frontend production build:

```bash
cd frontend
npm run build
```

## 14. Docker Compose local stack

This uses PostgreSQL so local behavior is closer to production.

```bash
docker compose up --build
```

Services:

- React: <http://localhost:5173>
- FastAPI: <http://localhost:8000>
- PostgreSQL: localhost:5432
- worker: background service

Stop:

```bash
docker compose down
```

Delete local DB volume too:

```bash
docker compose down -v
```

## 15. GCP production deployment model

Recommended shape:

1. React frontend → Cloud Run service
2. FastAPI API → Cloud Run service
3. Worker image → Cloud Run Job
4. Cloud SQL PostgreSQL → shared persistent job state/results
5. User-managed service account → Vertex AI + Cloud Run Job invocation permissions
6. Secret Manager → DB password
7. Artifact Registry → images

Cloud Run service identity provides ADC automatically. Do **not** set `GOOGLE_APPLICATION_CREDENTIALS` on Cloud Run.

### 15.1 One-time GCP bootstrap

Edit values first:

```bash
export PROJECT_ID="YOUR_PROJECT_ID"
export REGION="us-central1"
export REPO="seo-platform"
export SERVICE_ACCOUNT="seo-platform-runtime"

gcloud config set project "$PROJECT_ID"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="SEO platform containers" || true

gcloud iam service-accounts create "$SERVICE_ACCOUNT" \
  --display-name="SEO platform runtime" || true

RUNTIME_SA="$SERVICE_ACCOUNT@$PROJECT_ID.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/aiplatform.user"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/run.invoker"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/cloudsql.client"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/secretmanager.secretAccessor"
```

Your deployer also needs permission to attach the runtime service account, normally `roles/iam.serviceAccountUser` on that service account.

### 15.2 Cloud SQL

Create PostgreSQL in the region of your Cloud Run services. Example:

```bash
gcloud sql instances create seo-platform-db \
  --database-version=POSTGRES_16 \
  --region="$REGION" \
  --cpu=2 \
  --memory=7680MiB

gcloud sql databases create seo_platform --instance=seo-platform-db

gcloud sql users create seo_app \
  --instance=seo-platform-db \
  --password='REPLACE_WITH_STRONG_PASSWORD'
```

Store the password in Secret Manager instead of source control. For easiest deployment, create the complete SQLAlchemy URL as one secret after you know the instance connection name:

```bash
printf '%s' 'postgresql+psycopg://seo_app:REPLACE_WITH_URL_ENCODED_PASSWORD@/seo_platform?host=/cloudsql/PROJECT:REGION:INSTANCE' | \
  gcloud secrets create seo-database-url --data-file=- || \
  printf '%s' 'postgresql+psycopg://seo_app:REPLACE_WITH_URL_ENCODED_PASSWORD@/seo_platform?host=/cloudsql/PROJECT:REGION:INSTANCE' | \
  gcloud secrets versions add seo-database-url --data-file=-
```

Get the instance connection name:

```bash
gcloud sql instances describe seo-platform-db --format='value(connectionName)'
```

Build the SQLAlchemy URL as:

```text
postgresql+psycopg://seo_app:PASSWORD@/seo_platform?host=/cloudsql/PROJECT:REGION:INSTANCE
```

For production, inject the full `DATABASE_URL` using your secret-management process. Avoid printing secrets into CI logs.

### 15.3 Build backend image

```bash
export BACKEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/backend:latest"
gcloud builds submit backend --tag "$BACKEND_IMAGE"
```

### 15.4 Run migrations

The simplest controlled migration path is a temporary Cloud Run Job using the backend image and the same Cloud SQL connection/env. In a mature environment, run this in CI/CD before rollout.

Example command shape:

```bash
gcloud run jobs create seo-db-migrate \
  --image "$BACKEND_IMAGE" \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --add-cloudsql-instances "PROJECT:REGION:INSTANCE" \
  --set-secrets "DATABASE_URL=seo-database-url:latest" \
  --command alembic \
  --args upgrade,head || true

gcloud run jobs execute seo-db-migrate --region "$REGION" --wait
```

### 15.5 Create worker Cloud Run Job

```bash
gcloud run jobs create seo-analysis-worker \
  --image "$BACKEND_IMAGE" \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --cpu 2 \
  --memory 2Gi \
  --task-timeout 3600s \
  --max-retries 1 \
  --command python \
  --args=-m,app.worker,--once \
  --set-env-vars "APP_ENV=production,AI_MODE=vertex,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true,GEMINI_MODEL=gemini-2.5-flash,EMBEDDING_MODEL=gemini-embedding-001" \
  --set-secrets "DATABASE_URL=seo-database-url:latest" \
  --add-cloudsql-instances "PROJECT:REGION:INSTANCE" || true
```

Add `DATABASE_URL` securely as well.

### 15.6 Deploy FastAPI service

```bash
gcloud run deploy seo-api \
  --image "$BACKEND_IMAGE" \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --cpu 1 \
  --memory 1Gi \
  --add-cloudsql-instances "PROJECT:REGION:INSTANCE" \
  --set-secrets "DATABASE_URL=seo-database-url:latest" \
  --set-env-vars "APP_ENV=production,JOB_EXECUTION_MODE=cloud_run_job,CLOUD_RUN_PROJECT=$PROJECT_ID,CLOUD_RUN_REGION=$REGION,CLOUD_RUN_JOB_NAME=seo-analysis-worker,AI_MODE=vertex,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true"
```

Again inject `DATABASE_URL` securely.

Get API URL:

```bash
export API_URL="$(gcloud run services describe seo-api --region "$REGION" --format='value(status.url)')"
echo "$API_URL"
```

### 15.7 Build/deploy React frontend

```bash
export FRONTEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/frontend:latest"
(cd frontend && gcloud builds submit . \
  --config cloudbuild.yaml \
  --substitutions "_VITE_API_BASE_URL=$API_URL,_IMAGE=$FRONTEND_IMAGE")
```

If your Cloud Build configuration does not pass custom build args, build locally:

```bash
docker build --build-arg VITE_API_BASE_URL="$API_URL" -t "$FRONTEND_IMAGE" frontend
docker push "$FRONTEND_IMAGE"
```

Deploy:

```bash
gcloud run deploy seo-web \
  --image "$FRONTEND_IMAGE" \
  --region "$REGION" \
  --allow-unauthenticated
```

Then update FastAPI `FRONTEND_ORIGINS` with the deployed web URL and redeploy the API.

## 16. Security notes

- The API validates crawl targets as HTTP/HTTPS only.
- Hostnames resolving to loopback, private, link-local, multicast, reserved or unspecified addresses are blocked when `BLOCK_PRIVATE_NETWORKS=true`.
- Browser subresource requests are checked too, with per-host DNS caching during the crawl.
- Do not expose this crawler as a public anonymous arbitrary-URL service without authentication, quotas and tenant isolation.
- Cloud Run should use a dedicated user-managed service account with least privilege.
- Never commit `.env`, ADC files, OAuth refresh tokens, API keys or service-account private keys.
- Do not set `GOOGLE_APPLICATION_CREDENTIALS` in Cloud Run when using service identity.

## 17. Cost / performance controls

- Start with concurrency 2–3; raise slowly.
- Cache successful crawl payloads for 12 hours by default.
- Gemini is optional and only called once per completed run, not once per keyword × URL cell.
- Embeddings are only used for compact heading sets; deterministic token similarity is the fallback.
- Large projects should use PostgreSQL and Cloud Run Jobs, not inline execution.
- For 1000 URLs, split work across multiple job tasks only after adding a database-safe partitioning strategy.

## 18. Exact-match behavior

Phase 1 is deliberately deterministic: it does **normalized exact phrase matching**, not stemming and not semantic keyword matching. Semantic embeddings are used only in Phase 2 to compare heading themes when Vertex mode is available.

## 19. Useful commands

```bash
# Backend format/lint
ruff check backend/app backend/tests

# Tests
cd backend && pytest -q

# Re-run migration
cd backend && alembic upgrade head

# Reset local SQLite DB
rm -f backend/seo_platform.db
cd backend && alembic upgrade head

# Worker once (oldest queued run)
cd backend && python -m app.worker --once

# Worker specific run
cd backend && python -m app.worker --once --run-id 1
```


### One-command GCP deployment helper

After the one-time bootstrap, Cloud SQL creation, and `seo-database-url` secret creation:

```bash
export PROJECT_ID="YOUR_PROJECT_ID"
export REGION="us-central1"
export CLOUDSQL_CONNECTION="PROJECT:REGION:INSTANCE"
export DATABASE_URL_SECRET="seo-database-url"
chmod +x scripts/deploy_gcp.sh
./scripts/deploy_gcp.sh
```

The script builds the backend, runs Alembic migrations, creates the worker Cloud Run Job, deploys FastAPI, builds React with the deployed API URL, deploys React, and updates API CORS.
