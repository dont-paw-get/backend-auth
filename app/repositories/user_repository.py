import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import MemberStatus, User


class UserRepository:
    """member 테이블에 대한 조회 전용 접근을 담당한다."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """member_id(Cognito sub를 UUID로 저장한 값)로 MEMBER를 조회한다.

        CLIAR-87부터 member의 PK는 내부 BIGINT(id)이고, Cognito sub는
        별도의 UNIQUE 컬럼인 member_id(UUID)에 저장된다. 이 메서드는
        기존 이름(get_by_id)을 유지하되, 실제로는 member_id로 조회한다.
        """
        stmt = select(User).where(User.member_id == user_id).limit(1)
        return self.db.execute(stmt).scalar_one_or_none()

    def exists_by_email(self, email: str) -> bool:
        """주어진 이메일(정규화된 값)이 이미 존재하는지 확인한다."""
        stmt = select(User.member_id).where(User.email == email).limit(1)
        return self.db.execute(stmt).first() is not None

    def exists_by_nickname(self, nickname: str) -> bool:
        """주어진 닉네임이 이미 존재하는지 확인한다."""
        stmt = select(User.member_id).where(User.nickname == nickname).limit(1)
        return self.db.execute(stmt).first() is not None

    def exists_by_nickname_excluding_user_id(
        self, nickname: str, exclude_user_id: uuid.UUID
    ) -> bool:
        """
        본인(exclude_user_id, member_id)을 제외한 다른 MEMBER가 이미
        해당 닉네임을 사용 중인지 확인한다. 프로필 수정 시 "본인의
        기존 닉네임을 그대로 다시 보내는 경우"를 중복으로 오판하지
        않기 위해 사용한다.

        CLIAR-87에서 nickname UNIQUE 제약이 제거되었으므로, 이 메서드는
        더 이상 DB 제약 위반을 막기 위한 목적이 아니다. 다만 기존
        API 계약(PATCH /users/me에서 타인의 닉네임 재사용 시 409)을
        그대로 유지하기 위해 애플리케이션 레벨 검사로 존속시킨다.
        """
        stmt = (
            select(User.member_id)
            .where(User.nickname == nickname, User.member_id != exclude_user_id)
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

    def mark_withdrawn(self, member: User) -> User:
        """
        회원탈퇴 처리 1단계: status를 WITHDRAWN으로 변경한다.

        deleted_at은 건드리지 않는다(이 시점에서는 아직 Cognito 삭제가
        완료되지 않았으므로, "탈퇴 처리 중"과 "탈퇴 완료"를
        deleted_at 유무로 구분한다). add + flush까지만 담당하고
        commit/rollback은 호출자(service)의 책임으로 둔다.
        """
        member.status = MemberStatus.WITHDRAWN
        self.db.flush()
        return member

    def mark_deleted_now(self, member: User) -> User:
        """
        회원탈퇴 처리 2단계: Cognito 삭제 완료 후 deleted_at을 현재
        UTC 시각으로 기록한다. add + flush까지만 담당하고
        commit/rollback은 호출자(service)의 책임으로 둔다.
        """
        member.deleted_at = datetime.now(timezone.utc)
        self.db.flush()
        return member
