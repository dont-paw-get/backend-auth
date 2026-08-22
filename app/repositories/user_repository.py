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

    def exists_by_nickname_excluding_user_id(self, nickname: str, exclude_user_id: str) -> bool:
        """
        본인(exclude_user_id)을 제외한 다른 MEMBER가 이미 해당 닉네임을
        사용 중인지 확인한다. 프로필 수정 시 "본인의 기존 닉네임을
        그대로 다시 보내는 경우"를 중복으로 오판하지 않기 위해 사용한다.
        """
        stmt = (
            select(User.user_id)
            .where(User.nickname == nickname, User.user_id != exclude_user_id)
            .limit(1)
        )
        return self.db.execute(stmt).first() is not None

    def create(self, member: User) -> User:
        """
        새 MEMBER row를 생성한다.

        add + flush까지만 담당하고 commit/rollback은 호출자(service)의
        책임으로 둔다. 이렇게 하면 애플리케이션 사전 중복 검사를 통과한
        뒤에도 flush 시점에 발생할 수 있는 DB unique constraint 위반
        (경쟁 조건에 의한 최종 방어선)을 service 계층에서 하나의
        트랜잭션 경계 안에서 감지하고 롤백할 수 있다.
        """
        self.db.add(member)
        self.db.flush()
        return member
