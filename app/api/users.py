import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_member, get_member_by_sub
from app.core.cognito import (
    CognitoUserAlreadyDeletedError,
    delete_cognito_user,
    get_cognito_user_email,
)
from app.core.database import get_db
from app.core.security import bearer_scheme, get_current_access_token, get_current_user_id
from app.models.user import MemberStatus, User
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
    MemberWithdrawalPersistenceError,
    NicknameAlreadyExistsError,
    OnboardingData,
    RequiredConsentNotAgreedError,
    RequiredTermsNotConfiguredError,
    TrustedIdentity,
    bootstrap_member,
    complete_withdrawal,
    start_withdrawal,
)

logger = logging.getLogger(__name__)


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
        birth_date=payload.birth_date,
        gender=payload.gender,
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


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
)
def withdraw_current_member(
    member: User = Depends(get_member_by_sub),
    access_token: str = Depends(get_current_access_token),
    db: Session = Depends(get_db),
):
    """
    Cognito 연동 회원탈퇴 (CLIAR-113)

    request body는 받지 않는다. member_id/sub는 오직 검증된 Cognito
    Access Token(sub)에서 얻는다(get_member_by_sub -> get_current_user_id).

    get_current_member(ACTIVE 검사 포함)가 아니라 get_member_by_sub를
    사용하는 이유: Cognito DeleteUser가 실패해 재시도가 필요한 경우에도
    (status=WITHDRAWN, deleted_at=NULL) 이 endpoint가 자신의 member row를
    계속 조회할 수 있어야 하기 때문이다.

    처리 순서:
      1. status가 ACTIVE면 WITHDRAWN으로 변경하고 즉시 commit한다.
         이 시점부터 GET/PATCH /users/me는 403으로 차단된다.
      2. Cognito DeleteUser(access token 기반 self-service 삭제)를
         호출한다. AdminDeleteUser는 사용하지 않으며 IAM 권한도
         요구하지 않는다.
      3. Cognito 삭제가 성공하면(또는 이미 삭제되어 있었다고 안전하게
         판단되면) deleted_at을 현재 UTC 시각으로 기록하고 commit한다.

    재시도/멱등성:
      - status=WITHDRAWN, deleted_at=NULL: 이전 시도에서 Cognito 삭제
        또는 최종 DB 처리가 실패한 상태. 이 요청에서 다시 Cognito
        DeleteUser부터 재시도한다.
      - status=WITHDRAWN, deleted_at!=NULL: 이미 탈퇴 완료. 추가 DB
        변경 없이 204를 반환한다(Cognito를 다시 호출하지 않는다).
    """
    if member.status == MemberStatus.WITHDRAWN and member.deleted_at is not None:
        # 이미 탈퇴 완료된 상태. 추가 DB 변경도, Cognito 재호출도
        # 하지 않고 그대로 204를 반환한다(케이스 B).
        return None

    user_repository = UserRepository(db)

    if member.status != MemberStatus.WITHDRAWN:
        # 케이스: 최초 탈퇴 요청(ACTIVE -> WITHDRAWN, deleted_at은
        # 아직 NULL). 이 시점부터 일반 API 접근이 차단된다.
        try:
            start_withdrawal(member, user_repository)
        except MemberWithdrawalPersistenceError:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process withdrawal",
            )
    # else: 케이스 A(status=WITHDRAWN, deleted_at=NULL) — 이전 시도에서
    # 이미 WITHDRAWN까지는 반영됐으므로, 이 단계는 건너뛰고 Cognito
    # DeleteUser부터 재시도한다.

    try:
        delete_cognito_user(access_token, sub=str(member.member_id))
    except CognitoUserAlreadyDeletedError:
        # AWS 계약상 DeleteUser는 access token만으로 대상을 특정하므로,
        # UserNotFoundException은 "이 토큰이 가리키던 사용자가 이미
        # User Pool에 없다"로만 안전하게 해석할 수 있다(cognito.py 참고).
        # 이 경우에 한해 이미 삭제 완료로 간주하고 다음 단계로 진행한다.
        pass
    except ValueError:
        # 토큰 자체가 거절된 경우(만료/폐기 등)로, 사용자가 이미
        # 삭제되었는지 여부를 여기서 안전하게 판단할 수 없다. DB는
        # 이미 WITHDRAWN 상태이므로(재시도 가능), 명확한 401로 실패
        # 시키고 클라이언트가 필요 시 재인증 후 재시도하게 한다.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cognito rejected the access token",
        )
    except RuntimeError:
        # Cognito 측 장애/네트워크 문제. DB는 WITHDRAWN 상태로 남아
        # 재시도 가능하다.
        logger.error(
            "Withdrawal: Cognito DeleteUser call failed for sub=%s",
            member.member_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete the Cognito user account",
        )

    try:
        complete_withdrawal(member, user_repository)
    except MemberWithdrawalPersistenceError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to finalize withdrawal",
        )

    return None