from fastapi import APIRouter, Depends

from app.api.deps import get_current_member
from app.models.user import User
from app.schemas.user import MemberResponse

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
