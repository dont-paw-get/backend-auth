from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User


class UserRepository:
    """member 테이블에 대한 조회 전용 접근을 담당한다."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, user_id: str) -> User | None:
        """user_id(Cognito sub)로 MEMBER를 조회한다. 없으면 None을 반환한다."""
        return self.db.get(User, user_id)

    def exists_by_email(self, email: str) -> bool:
        """주어진 이메일(정규화된 값)이 이미 존재하는지 확인한다."""
        stmt = select(User.user_id).where(User.email == email).limit(1)
        return self.db.execute(stmt).first() is not None

    def exists_by_nickname(self, nickname: str) -> bool:
        """주어진 닉네임이 이미 존재하는지 확인한다."""
        stmt = select(User.user_id).where(User.nickname == nickname).limit(1)
        return self.db.execute(stmt).first() is not None
