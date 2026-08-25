import uuid

from sqlalchemy.orm import Session

from app.models.member_agreement import MemberAgreement, MemberAgreementAction


class MemberAgreementRepository:
    """member_agreement 테이블에 대한 이력 추가 전용 접근을 담당한다."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        member_id: uuid.UUID,
        terms_id: int,
        action: MemberAgreementAction,
    ) -> MemberAgreement:
        """
        새 동의/철회 이력 행을 추가한다.

        add + flush까지만 담당하고 commit/rollback은 호출자(service)의
        책임으로 둔다(app/repositories/user_repository.py의 create와
        동일한 패턴). occurred_at은 명시적으로 지정하지 않아 컬럼의
        server_default(now())가 적용되며, 클라이언트가 보낸 시간을
        신뢰하지 않는다.
        """
        agreement = MemberAgreement(
            member_id=member_id,
            terms_id=terms_id,
            action=action,
        )
        self.db.add(agreement)
        self.db.flush()
        return agreement
