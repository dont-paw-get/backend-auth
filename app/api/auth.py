from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    AvailabilityRequest,
    AvailabilityResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
)
from app.services.auth_service import check_availability, refresh_access_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/availability", response_model=AvailabilityResponse)
def check_availability_endpoint(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """
    회원가입 전 이메일/닉네임 중복 확인.

    Jira 요구사항상 잘못된 요청(지원하지 않는 field, 빈/공백 value,
    필수 키 누락 등)은 모두 HTTP 400이어야 한다. FastAPI/Pydantic의
    기본 body validation을 그대로 사용하면 422가 반환되므로,
    여기서는 raw dict로 body를 받아 직접 검증하여 400으로 통일한다.
    """
    try:
        request = AvailabilityRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user_repository = UserRepository(db)
    return check_availability(request.field, request.value, user_repository)


@router.post("/refresh", response_model=RefreshTokenResponse)
def refresh_token_endpoint(payload: RefreshTokenRequest):
    """
    Cognito Refresh Token으로 새 Access Token을 재발급한다 (CLIAR-125).

    Access Token이 만료됐을 때 사용하는 API이므로, 이 endpoint는
    Bearer Access Token 인증을 요구하지 않는다(users 라우터와 달리
    bearer_scheme 의존성을 두지 않음). client가 보낸 refresh_token의
    유효성 자체는 Cognito가 판단하며, 이 코드는 그 결과를 그대로
    신뢰하고 재검증(JWT 서명 등)을 시도하지 않는다.
    """
    try:
        return refresh_access_token(payload.refresh_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cognito rejected the refresh token",
        )
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not refresh the access token",
        )
