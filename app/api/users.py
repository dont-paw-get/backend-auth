from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_member
from app.core.database import get_db
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import MemberResponse, MemberUpdateRequest

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/me", response_model=MemberResponse)
def read_current_member(current_member: User = Depends(get_current_member)):
    """
    현재 인증된 사용자의 MEMBER 정보를 조회한다.

    인증 사용자 식별은 기존 get_current_member(Depends 체인:
    get_current_user_id -> UserRepository.get_by_id)를 그대로 재사용하며,
    이 엔드포인트에서 Cognito sub 처리 로직을 다시 구현하지 않는다.
    """
    return current_member


@router.patch("/me", response_model=MemberResponse)
def update_current_member(
    payload: MemberUpdateRequest,
    current_member: User = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    """
    현재 인증된 사용자의 프로필(nickname/profile_image_url/agree_ai_analysis)을
    부분 수정한다.

    인증 사용자 식별은 GET /me와 동일하게 get_current_member를 그대로
    재사용한다. 요청에 실제로 포함된 필드만 model_dump(exclude_unset=True)
    로 구분해 적용하므로, 요청에 없는 필드는 그대로 유지된다.
    """
    user_repository = UserRepository(db)
    updates = payload.model_dump(exclude_unset=True)

    if "nickname" in updates:
        new_nickname = updates["nickname"]
        if user_repository.exists_by_nickname_excluding_user_id(
            new_nickname, exclude_user_id=current_member.user_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Nickname is already in use by another member",
            )

    for field, value in updates.items():
        setattr(current_member, field, value)

    db.commit()
    db.refresh(current_member)
    return current_member
