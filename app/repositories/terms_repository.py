from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.terms import Terms


class TermsRepository:
    """terms 테이블에 대한 조회 전용 접근을 담당한다."""

    def __init__(self, db: Session):
        self.db = db

    def get_current_by_code(self, code: str) -> Terms | None:
        """
        주어진 code에 대해 현재 적용 중인 약관을 조회한다.

        "현재 적용 중"의 기준:
        - code가 일치
        - deleted_at IS NULL
        - expired_at IS NULL
        - effective_at <= 현재 시각

        같은 code에 대해 현재 적용 가능한 행이 여러 개 존재하면
        (정상 운영에서는 발생하지 않아야 하지만) 가장 최근에
        effective_at이 시작된 행을 사용한다. 없으면 None을 반환한다.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            select(Terms)
            .where(
                Terms.code == code,
                Terms.deleted_at.is_(None),
                Terms.expired_at.is_(None),
                Terms.effective_at <= now,
            )
            .order_by(Terms.effective_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()
