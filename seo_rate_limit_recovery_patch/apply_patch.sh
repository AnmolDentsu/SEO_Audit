#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$HOME/Downloads/seo-ai-platform-service-account}"
PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$ROOT/patch-backup-$STAMP"

FILES=(
  backend/app/config.py
  backend/app/api.py
  backend/app/services/crawler.py
  backend/app/services/sitemap_fallback.py
  backend/app/services/sitemap_job_runner.py
  backend/app/services/sitemap_page_resolver.py
  backend/app/services/sitemap_result_views.py
  backend/app/services/sitemap_recovery.py
  backend/scripts/recover_failed_sitemap_pages.py
  backend/tests/test_sitemap_fast_fallback.py
  backend/tests/test_sitemap_rate_limit_scheduler.py
  frontend/src/api.js
  frontend/src/SitemapIntelligence.jsx
  frontend/src/styles.css
)

if [[ ! -d "$ROOT/backend/app" || ! -d "$ROOT/frontend/src" ]]; then
  echo "Project root not found: $ROOT" >&2
  exit 1
fi

mkdir -p "$BACKUP"
for f in "${FILES[@]}"; do
  if [[ -f "$ROOT/$f" ]]; then
    mkdir -p "$BACKUP/$(dirname "$f")"
    cp "$ROOT/$f" "$BACKUP/$f"
  fi
  mkdir -p "$ROOT/$(dirname "$f")"
  cp "$PATCH_DIR/$f" "$ROOT/$f"
done

echo "Patch copied successfully."
echo "Backup: $BACKUP"
echo "No database migration is required by this patch."
