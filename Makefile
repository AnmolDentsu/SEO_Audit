.PHONY: backend-install frontend-install migrate api worker test build docker-up docker-down

backend-install:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -e ".[dev]" && playwright install chromium

frontend-install:
	cd frontend && npm install

migrate:
	cd backend && . .venv/bin/activate && alembic upgrade head

api:
	cd backend && . .venv/bin/activate && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:
	cd backend && . .venv/bin/activate && python -m app.worker

test:
	cd backend && . .venv/bin/activate && pytest -q

build:
	cd frontend && npm run build

docker-up:
	docker compose up --build

docker-down:
	docker compose down
