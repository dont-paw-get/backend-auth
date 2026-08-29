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

    def get_by_email(self, email: str) -> User | None:
        """
        정규화된 email로 "현재 유효한"(탈퇴 완료되지 않은) MEMBER를
        조회한다.

        deleted_at IS NULL인 행만 대상으로 한다. 탈퇴 완료(deleted_at
        설정됨)된 회원의 이메일은 재가입을 허용하므로(CLIAR-177),
        같은 email로 여러 WITHDRAWN 이력 행이 존재할 수 있다 — 이
        필터가 없으면 그중 임의의 과거 행이 반환될 수 있다(ORDER BY가
        없는 LIMIT 1이므로 어떤 행이 반환될지 결정론적이지 않다).
        deleted_at IS NULL인 행은 uq_member_email_active partial
        unique index(같은 이유로 app/models/user.py 참고)에 의해
        email당 최대 1건만 존재할 수 있으므로 이 조회는 항상
        결정론적이다.

        호출자는 auth_service._normalize_email과 동일하게 strip +
        lower를 적용한 값을 넘겨야 한다(이 메서드는 정규화를 수행하지
        않는다).

        status로는 필터링하지 않는다(ACTIVE/PENDING/WITHDRAWN-진행중
        모두 "아직 유효한" 것으로 취급 — deleted_at만이 재가입 가능
        여부를 결정한다). 상태에 따른 추가 판단은 호출자(service)의
        책임이다.
        """
        stmt = (
            select(User)
            .where(User.email == email, User.deleted_at.is_(None))
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def exists_by_email(self, email: str) -> bool:
        """
        주어진 이메일(정규화된 값)을 현재 유효한(탈퇴 완료되지 않은)
        회원이 사용 중인지 확인한다.

        deleted_at IS NULL인 행만 "사용 중"으로 계산한다(CLIAR-177).
        탈퇴가 완료된(deleted_at 설정됨) 회원의 이메일은 재가입 가능
        해야 하므로 여기서 제외한다.

        status로는 필터링하지 않는 것은 의도적이다. PENDING(이메일
        인증 대기) 회원의 이메일도 Cognito User Pool에서 이미 점유된
        상태이므로, 같은 이메일로의 신규 가입은 어차피
        UsernameExistsException으로 실패한다. 마찬가지로 탈퇴 처리가
        아직 완료되지 않은(status=WITHDRAWN이지만 deleted_at=NULL,
        즉 Cognito DeleteUser가 아직 확정되지 않은) 회원도 Cognito
        쪽 계정이 남아있을 수 있으므로 "사용 중"으로 취급한다.
        """
        stmt = (
            select(User.member_id)
            .where(User.email == email, User.deleted_at.is_(None))
            .limit(1)
        )
        return self.db.execute(stmt).first() is not None

    def exists_by_nickname(self, nickname: str) -> bool:
        """주어진 닉네임이 이미 존재하는지 확인한다."""
        stmt = select(User.member_id).where(User.nickname == nickname).limit(1)
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
