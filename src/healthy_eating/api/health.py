from fastapi import APIRouter, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError

from healthy_eating.db import Database

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, str]:
    """Liveness and database connectivity check."""
    db: Database = request.app.state.db
    try:
        await db.ping()
    except (SQLAlchemyError, OSError):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
