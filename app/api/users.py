import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_member
from app.core.cognito import get_cognito_user_email
from app.core.database import get_db
from app.core.security import bearer_scheme, get_current_access_token, get_current_user_id
from app.models.user import User
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    MemberBootstrapRequest,
    MemberResponse,
    MemberUpdateRequest,
)
from app.services.member_service import (
    EmailAlreadyExistsError,
    InvalidNicknameError,
    MemberAlreadyExistsError,
    NicknameAlreadyExistsError,
    OnboardingData,
    RequiredConsentNotAgreedError,
    RequiredTermsNotConfiguredError,
    TrustedIdentity,
    bootstrap_member,
)


router = APIRouter(
    prefix="/api/v1/users",
    tags=["users"],
    # Swagger UI 상단 "Authorize"에서 Bearer Access Token을 입력할 수
    # 있도록 노출한다(GET/PATCH /me, POST /bootstrap 모두 적용).
    # 실제 인증/거부 로직은 여전히 get_current_user_id 등이 담당한다.
    dependencies=[Depends(bearer_scheme)],
)



@router.get("/me", response_model=MemberResponse)
def read_current_member(
    current_member: User = Depends(get_current_member),
):
    """
    현재 인증된 MEMBER 정보 조회
    """

    return current_member



@router.patch("/me", response_model=MemberResponse)
def update_current_member(
    payload: MemberUpdateRequest,
    current_member: User = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    """
    현재 MEMBER 정보 수정
    """

    # CLIAR-87: member.nickname은 UNIQUE 제약이 없으며 중복을 허용한다.
    # 따라서 프로필 수정 시 다른 회원과 동일한 nickname으로 변경해도
    # 409를 반환하지 않는다.
    updates = payload.model_dump(
        exclude_unset=True
    )


    for field, value in updates.items():
        setattr(
            current_member,
            field,
            value,
        )


    db.commit()
    db.refresh(current_member)

    return current_member




@router.post(
    "/bootstrap",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def bootstrap_current_member(
    payload: MemberBootstrapRequest,
    user_id: str = Depends(get_current_user_id),
    access_token: str = Depends(get_current_access_token),
    db: Session = Depends(get_db),
):
    """
    Cognito 인증 완료 후 MEMBER 최초 생성

    CLIAR-105: member_id(Cognito sub)와 email은 client request body가
    아니라 Authorization의 검증된 Cognito Access Token(sub)과 Cognito
    GetUser 응답(email)에서만 얻는다. GET/PATCH /users/me와 동일한
    get_current_user_id 인증 경로를 재사용한다.
    """

    try:
        member_id = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated identity is not a valid UUID",
        )

    try:
        email = get_cognito_user_email(access_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cognito rejected the access token",
        )
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not retrieve user information from Cognito",
        )

    repository = UserRepository(db)
    terms_repository = TermsRepository(db)
    member_agreement_repository = MemberAgreementRepository(db)


    identity = TrustedIdentity(
        user_id=member_id,
        email=email,
    )


    onboarding = OnboardingData(
        nickname=payload.nickname,
        agree_terms=payload.agree_terms,
        agree_privacy=payload.agree_privacy,
        agree_ai_analysis=payload.agree_ai_analysis,
    )


    try:

        member = bootstrap_member(
            identity,
            onboarding,
            repository,
            terms_repository,
            member_agreement_repository,
        )


    except (
        MemberAlreadyExistsError,
        EmailAlreadyExistsError,
        NicknameAlreadyExistsError,
    ) as e:

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


    except (
        InvalidNicknameError,
        RequiredConsentNotAgreedError,
    ) as e:

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


    except RequiredTermsNotConfiguredError as e:
        # 사용자 입력 문제가 아니라 서버(운영) 설정 문제이므로 400/409가
        # 아니라 서버 오류로 응답한다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )


    return member