#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .env ] || cp .env.example .env
python3 -m venv backend/.venv
source backend/.venv/bin/activate
python -m pip install --upgrade pip
pip install -e "./backend[dev]"
python -m playwright install chromium
cd backend
alembic upgrade head
cd ../frontend
[ -f .env ] || cp .env.example .env
npm install
printf '\nSetup complete.\nRun API:    cd backend && source .venv/bin/activate && uvicorn app.main:app --reload --port 8000\nRun worker: cd backend && source .venv/bin/activate && python -m app.worker\nRun web:    cd frontend && npm run dev\n'
