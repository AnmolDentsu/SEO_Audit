import logging

from fastapi import FastAPI

from fastapi.middleware.cors import (
    CORSMiddleware,
)

from sqlalchemy import text

from app.api import router
from app.queue_api import router as queue_router
from app.config import get_settings
from app.database import engine


settings = get_settings()


logging.basicConfig(
    level=getattr(
        logging,
        settings.log_level.upper(),
        logging.INFO,
    ),
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(name)s "
        "%(message)s"
    ),
)


app = FastAPI(
    title="SEO AI Platform API",
    version="0.1.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(
    router
)

app.include_router(
    queue_router
)


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "service": "seo-ai-platform-api",
    }


@app.get("/readyz")
def readyz():
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT 1"
                )
            )

        return {
            "status": "ready",
            "database": "ok",
            "ai_mode": settings.ai_mode,
        }

    except Exception as exc:
        return {
            "status": "not_ready",
            "database": "error",
            "detail": str(exc),
        }
