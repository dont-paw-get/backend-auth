from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_member
from app.core.database import get_db
from app.models.user import User
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
    TrustedIdentity,
    bootstrap_member,
)


router = APIRouter(
    prefix="/api/v1/users",
    tags=["users"],
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
    db: Session = Depends(get_db),
):
    """
    Cognito 인증 완료 후 MEMBER 최초 생성

    Cognito sub(user_id) + onboarding 정보
    -> MEMBER 생성
    """

    repository = UserRepository(db)


    identity = TrustedIdentity(
        user_id=payload.user_id,
        email=payload.email,
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


    return member