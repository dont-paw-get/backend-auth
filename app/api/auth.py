import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.cognito_errors import CognitoApiError
from app.core.database import get_db
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    AvailabilityRequest,
    AvailabilityResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    SignupConfirmRequest,
    SignupConfirmResponse,
    SignupRequest,
    SignupResendRequest,
    SignupResponse,
)
from app.services.auth_service import check_availability, refresh_access_token
from app.services.member_service import (
    InvalidNicknameError,
    RequiredConsentNotAgreedError,
    RequiredTermsNotConfiguredError,
)
from app.services.signup_service import (
    ConfirmPersistenceError,
    EmailAlreadyRegisteredError,
    MemberNotFoundForConfirmError,
    SignupData,
    SignupPersistenceError,
    confirm_signup,
    resend_signup_code,
    sign_up_member,
)

logger = logging.getLogger(__name__)

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


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
)
def signup_endpoint(
    payload: SignupRequest,
    db: Session = Depends(get_db),
):
    """
    Cognito SignUp + RDS member(PENDING) 생성 (CLIAR-151, PLAN.md §5).

    이 endpoint는 Bearer Access Token 인증을 요구하지 않는다(아직
    로그인하지 않은 사용자가 호출하는 API이기 때문). 실제 오케스트
    레이션(Cognito 호출, 이메일 중복 확인, 필수 약관 조회, member/
    member_agreement 생성, 실패 시 보상)은 전부
    app/services/signup_service.py가 담당하며, 이 함수는 request
    parsing과 예외 -> HTTP status 변환만 수행한다.

    CLIAR-144 정책: nickname 중복 검사를 하지 않는다.
    """
    user_repository = UserRepository(db)
    terms_repository = TermsRepository(db)
    member_agreement_repository = MemberAgreementRepository(db)

    data = SignupData(
        email=payload.email,
        password=payload.password.get_secret_value(),
        nickname=payload.nickname,
        birth_date=payload.birth_date,
        gender=payload.gender,
        agree_terms=payload.agree_terms,
        agree_privacy=payload.agree_privacy,
        agree_ai_analysis=payload.agree_ai_analysis,
    )

    try:
        member = sign_up_member(
            data,
            user_repository,
            terms_repository,
            member_agreement_repository,
        )
    except EmailAlreadyRegisteredError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except (InvalidNicknameError, RequiredConsentNotAgreedError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RequiredTermsNotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        )
    except SignupPersistenceError as e:
        if e.compensation_failed:
            logger.error(
                "Signup DB failure and Cognito compensation (AdminDeleteUser) "
                "also failed; a possible orphan Cognito account remains "
                "for email hash unavailable here (see service logs)"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except CognitoApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    return SignupResponse(
        member_id=member.member_id,
        email=member.email,
        status=member.status.value,
        code_delivery=None,
    )


@router.post("/signup/confirm", response_model=SignupConfirmResponse)
def signup_confirm_endpoint(
    payload: SignupConfirmRequest,
    db: Session = Depends(get_db),
):
    """
    Cognito ConfirmSignUp + RDS member(PENDING -> ACTIVE) 전이
    (CLIAR-151, PLAN.md §5).

    Cognito ConfirmSignUp이 성공한 뒤 DB UPDATE가 실패하면
    ConfirmPersistenceError -> 500으로 응답한다(성공한 것처럼 200을
    반환하지 않는다). DB에 해당 email의 member가 없으면
    MemberNotFoundForConfirmError -> 404로 응답한다.
    """
    user_repository = UserRepository(db)

    try:
        member = confirm_signup(
            email=payload.email,
            code=payload.code,
            user_repository=user_repository,
        )
    except MemberNotFoundForConfirmError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConfirmPersistenceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except CognitoApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    return SignupConfirmResponse(
        member_id=member.member_id,
        email=member.email,
        status=member.status.value,
    )


@router.post("/signup/resend", status_code=status.HTTP_204_NO_CONTENT)
def signup_resend_endpoint(payload: SignupResendRequest):
    """
    Cognito ResendConfirmationCode 재호출 (CLIAR-151, PLAN.md §5).

    사용자 존재 여부를 과도하게 노출하지 않는 기존 보안 정책은
    app/core/cognito_errors.py의 매핑 테이블(NotAuthorizedException/
    UserNotFoundException 동일 응답)에 이미 반영되어 있다.
    """
    try:
        resend_signup_code(email=payload.email)
    except CognitoApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    return None
