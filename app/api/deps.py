"""
FastAPI dependency로 조합된 "현재 인증 사용자" 조회 기반.

흐름:
    get_current_user_id (app/core/security.py)
        -> Cognito sub 획득
    get_current_member (여기)
        -> sub(=user_id)로 UserRepository를 통해 MEMBER 조회
        -> MEMBER가 없으면 404
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

import uuid

from app.core.database import get_db
from app.core.security import get_current_user_id
from app.models.user import User
from app.repositories.user_repository import UserRepository


def get_current_member(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> User:
    """
    인증된 Cognito sub에 해당하는 MEMBER를 조회한다.

    다른 API는 이 dependency를 통해 "현재 로그인한 사용자"를
    바로 얻을 수 있다. 회원가입 전(Cognito 계정은 있지만 MEMBER
    레코드가 아직 없는 상태) 요청에 대해서는 404로 명확히 구분한다.

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
