import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultlab.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    response: Response,
    session: SessionDependency,
) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness probe failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready"}
    return {"status": "ready"}
