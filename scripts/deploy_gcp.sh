#!/usr/bin/env bash
set -euo pipefail
: "${PROJECT_ID:?Set PROJECT_ID}"
: "${CLOUDSQL_CONNECTION:?Set CLOUDSQL_CONNECTION, e.g. project:region:instance}"
: "${DATABASE_URL_SECRET:?Set DATABASE_URL_SECRET to a Secret Manager secret containing the complete SQLAlchemy DATABASE_URL}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-seo-platform}"
RUNTIME_SA_NAME="${SERVICE_ACCOUNT:-seo-platform-runtime}"
RUNTIME_SA="$RUNTIME_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
BACKEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/backend:latest"
FRONTEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/frontend:latest"

gcloud config set project "$PROJECT_ID"

echo "Building backend..."
gcloud builds submit backend --tag "$BACKEND_IMAGE"

echo "Creating/updating migration job..."
gcloud run jobs delete seo-db-migrate --region "$REGION" --quiet >/dev/null 2>&1 || true
gcloud run jobs create seo-db-migrate \
  --image "$BACKEND_IMAGE" \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --add-cloudsql-instances "$CLOUDSQL_CONNECTION" \
  --set-secrets "DATABASE_URL=$DATABASE_URL_SECRET:latest" \
  --command alembic \
  --args upgrade,head

gcloud run jobs execute seo-db-migrate --region "$REGION" --wait

echo "Creating/updating analysis worker job..."
gcloud run jobs delete seo-analysis-worker --region "$REGION" --quiet >/dev/null 2>&1 || true
gcloud run jobs create seo-analysis-worker \
  --image "$BACKEND_IMAGE" \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --cpu 2 --memory 2Gi --task-timeout 3600s --max-retries 1 \
  --add-cloudsql-instances "$CLOUDSQL_CONNECTION" \
  --set-secrets "DATABASE_URL=$DATABASE_URL_SECRET:latest" \
  --set-env-vars "APP_ENV=production,AI_MODE=vertex,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true,GEMINI_MODEL=gemini-2.5-flash,EMBEDDING_MODEL=gemini-embedding-001" \
  --command python \
  --args=-m,app.worker,--once

echo "Deploying API..."
gcloud run deploy seo-api \
  --image "$BACKEND_IMAGE" \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --cpu 1 --memory 1Gi \
  --add-cloudsql-instances "$CLOUDSQL_CONNECTION" \
  --set-secrets "DATABASE_URL=$DATABASE_URL_SECRET:latest" \
  --set-env-vars "APP_ENV=production,JOB_EXECUTION_MODE=cloud_run_job,CLOUD_RUN_PROJECT=$PROJECT_ID,CLOUD_RUN_REGION=$REGION,CLOUD_RUN_JOB_NAME=seo-analysis-worker,AI_MODE=vertex,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true"

API_URL="$(gcloud run services describe seo-api --region "$REGION" --format='value(status.url)')"
echo "API URL: $API_URL"

echo "Building frontend with API URL..."
(
  cd frontend
  gcloud builds submit . \
    --config cloudbuild.yaml \
    --substitutions "_VITE_API_BASE_URL=$API_URL,_IMAGE=$FRONTEND_IMAGE"
)

echo "Deploying frontend..."
gcloud run deploy seo-web \
  --image "$FRONTEND_IMAGE" \
  --region "$REGION" \
  --allow-unauthenticated
WEB_URL="$(gcloud run services describe seo-web --region "$REGION" --format='value(status.url)')"

echo "Updating API CORS..."
gcloud run services update seo-api --region "$REGION" --update-env-vars "FRONTEND_ORIGINS=$WEB_URL"

echo "Deployment complete"
echo "Web: $WEB_URL"
echo "API: $API_URL"
