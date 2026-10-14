from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from sqlalchemy.orm import Session

from app.database import get_db
from app.services.queue_eta import (
    queue_status,
)


router = APIRouter(
    prefix="/api/queue",
    tags=["queue"],
)


@router.get("/status")
def get_queue_status(
    kind: str,
    run_id: int | None = None,
    db: Session = Depends(
        get_db
    ),
):
    try:
        return queue_status(
            db,
            kind,
            run_id,
        )

    except LookupError as exc:
        raise HTTPException(
            404,
            str(exc),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            422,
            str(exc),
        ) from exc