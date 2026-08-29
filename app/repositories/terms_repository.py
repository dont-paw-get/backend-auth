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

    def list_current(self) -> list[Terms]:
        """
        현재 적용 중인 모든 약관을 code 무관하게 조회한다
        (CLIAR-176, GET /api/v1/terms).

        "현재 적용 중"의 기준은 get_current_by_code와 완전히 동일하다
        (deleted_at IS NULL, expired_at IS NULL, effective_at <= 현재
        시각) — 두 메서드가 서로 다른 기준으로 "현재"를 판단하면
        signup이 참조하는 약관과 이 목록 조회가 보여주는 약관이
        어긋날 수 있으므로, 하나의 정의를 공유한다.

        row가 하나도 없어도(테이블이 비어 있거나 일부 code만 존재)
        예외를 던지지 않고 빈 목록을 반환한다 — 존재 여부 판단과
        그에 따른 정책(예: 필수 약관 누락 시 503)은 호출자의 책임이다.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            select(Terms)
            .where(
                Terms.deleted_at.is_(None),
                Terms.expired_at.is_(None),
                Terms.effective_at <= now,
            )
            .order_by(Terms.effective_at.desc())
        )
        return list(self.db.execute(stmt).scalars())
