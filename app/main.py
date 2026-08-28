from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.mock import router as mock_router
from app.api.users import router as users_router
from app.core.config import settings


def configure_cors(app: FastAPI) -> None:
    """
    CORS 미들웨어를 등록한다 (CLIAR-153, PLAN.md §8.4).

    refresh_token/refresh_sub는 HttpOnly 쿠키로 전달되므로, FE가
    다른 origin에서 backend-auth를 호출하려면
    allow_credentials=True가 필수다. 브라우저는
    allow_credentials=True와 와일드카드 origin을 함께 허용하지
    않으므로, 허용 origin은 settings.cors_allowed_origins_list에서
    명시적으로만 받는다("*"는 그 property가 걸러낸다).

    아직 CORS_ALLOWED_ORIGINS가 주입되지 않은 환경에서는 목록이
    비어 있고, 그 경우 어떤 cross-origin 요청도 허용되지 않는다
    (= CORS 미들웨어가 없던 기존 동작과 동일). startup은 깨지지
    않는다. 실제 운영 origin은 코드에 하드코딩하지 않는다.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


app = FastAPI(title="dont-paw-get auth service")

configure_cors(app)

app.include_router(health_router)
app.include_router(mock_router)
app.include_router(auth_router)
app.include_router(users_router)
