from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.mock import router as mock_router
from app.api.users import router as users_router

app = FastAPI(title="dont-paw-get auth service")

app.include_router(health_router)
app.include_router(mock_router)
app.include_router(auth_router)
app.include_router(users_router)
