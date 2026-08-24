from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/mock/ping")
def mock_ping():
    return {
        "message": "pong",
        "env": settings.APP_ENV,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
