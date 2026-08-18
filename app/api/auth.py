from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.user_repository import UserRepository
from app.schemas.auth import AvailabilityRequest, AvailabilityResponse
from app.services.auth_service import check_availability

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
