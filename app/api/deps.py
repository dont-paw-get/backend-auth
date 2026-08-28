"""
FastAPI dependency로 조합된 "현재 인증 사용자" 조회 기반.

흐름:
    get_current_user_id (app/core/security.py)
        -> Cognito sub 획득
    _get_member_by_sub (여기, 내부 helper)
        -> sub(=user_id)로 UserRepository를 통해 MEMBER 조회
        -> MEMBER가 없으면 404, sub가 UUID가 아니면 401
        -> status/deleted_at은 검사하지 않는다
    get_current_member (여기)
        -> _get_member_by_sub 조회 결과에 더해, 아직 사용할 수 없는
           회원의 일반 API 접근을 403으로 차단한다.
           GET/PATCH /users/me가 사용한다.
           - WITHDRAWN 처리됨(status=WITHDRAWN 또는 deleted_at 설정)
             -> 403, detail은 문자열(기존 계약 유지)
           - 이메일 인증 미완료(status=PENDING)
             -> 403, detail은 {"code": "EMAIL_NOT_VERIFIED", ...}
    get_member_by_sub (여기)
        -> _get_member_by_sub와 동일하게 status 검사 없이 조회만
           수행하는 FastAPI dependency. CLIAR-113 회원탈퇴
           (DELETE /users/me)는 재시도를 위해 WITHDRAWN 상태에서도
           자신의 member row를 조회할 수 있어야 하므로, 그 경로는
           get_current_member(ACTIVE 검사 포함)가 아니라 이 dependency를
           사용한다.

get_current_member의 시그니처(user_id, db를 직접 Depends받는 형태)는
기존 테스트(tests/test_current_member.py)가 이 함수를 FastAPI 없이
직접 호출하는 방식과 호환되도록 그대로 유지한다.
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

import uuid

from app.core.database import get_db
from app.core.security import get_current_user_id
from app.models.user import MemberStatus, User
from app.repositories.user_repository import UserRepository


def _lookup_member_by_sub(user_id: str, db: Session) -> User:
    """
    인증된 Cognito sub에 해당하는 MEMBER를 status와 무관하게 조회한다.

    회원가입 전(Cognito 계정은 있지만 MEMBER 레코드가 아직 없는 상태)
    요청에 대해서는 404로 명확히 구분한다. WITHDRAWN 상태의 회원도
    여기서는 그대로 반환한다(status 검사는 호출자의 책임이다).

    CLIAR-87: Cognito sub는 JWT claim에서 문자열로 전달되지만,
    member.member_id는 UUID 컬럼이다. 여기서 UUID로 파싱하며, sub가
    UUID 형식이 아니면(=Cognito 연결이 예상과 다른 상태) 조용히
    임의의 값으로 대체하지 않고 명확히 401로 실패시킨다.
    """
    try:
        member_id = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated identity is not a valid UUID",
        )

    user_repository = UserRepository(db)
    member = user_repository.get_by_id(member_id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found for the authenticated user",
        )
    return member


def get_current_member(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> User:
    """
    인증된 Cognito sub에 해당하는, 아직 사용 가능한(WITHDRAWN 처리되지
    않은) MEMBER를 조회한다.

    GET/PATCH /users/me 등 일반 회원 API는 이 dependency를 통해
    "현재 로그인한, 정상 이용 가능한 사용자"를 얻는다. member row 자체가
    없는 경우는 기존과 동일하게 404를 유지한다. member row는 있지만
    회원탈퇴 처리로 status=WITHDRAWN이거나 deleted_at이 설정된 경우
    (CLIAR-113), 인증 토큰 자체는 유효하지만 더 이상 사용할 수 없는
    계정이므로 403으로 차단한다.
    """
    member = _lookup_member_by_sub(user_id, db)

    # 탈퇴 검사를 먼저 한다. 탈퇴는 종착 상태이므로, 어떤 이유로든
    # status가 PENDING인 채로 deleted_at이 찍힌 row가 있더라도
    # "이메일 인증 필요"가 아니라 "탈퇴한 계정"으로 응답해야 한다.
    if member.status == MemberStatus.WITHDRAWN or member.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This member has been withdrawn",
        )

    # PENDING = Cognito SignUp은 됐지만 이메일 인증이 끝나지 않은 상태.
    # 정상 경로에서는 Cognito가 미확인 계정의 InitiateAuth를 거부하므로
    # 여기까지 오지 않는다. 다만 ConfirmSignUp은 성공했는데 뒤이은 DB
    # UPDATE가 실패해 Cognito=CONFIRMED / DB=PENDING으로 어긋난 경우
    # 유효한 access token을 가진 PENDING 회원이 존재할 수 있다.
    #
    # detail을 문자열이 아니라 code를 담은 dict로 반환하는 이유: FE가
    # "탈퇴한 계정"(재가입 안내)과 "이메일 인증 미완료"(인증 코드 입력
    # 화면으로 이동)를 구분해서 라우팅해야 하기 때문이다. 기존
    # WITHDRAWN 응답은 API 계약을 깨지 않도록 문자열 그대로 둔다.
    if member.status == MemberStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "EMAIL_NOT_VERIFIED",
                "message": "Email verification has not been completed",
            },
        )

    return member


def get_member_by_sub(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> User:
    """
    인증된 Cognito sub에 해당하는 MEMBER를 status와 무관하게 조회하는
    FastAPI dependency(CLIAR-113).

    get_current_member와 달리 WITHDRAWN/삭제 상태를 403으로 차단하지
    않는다. DELETE /users/me가 재시도(status=WITHDRAWN, deleted_at=NULL)
    케이스에서도 자신의 member row를 조회할 수 있도록 이 dependency를
    사용한다.
    """
    return _lookup_member_by_sub(user_id, db)
