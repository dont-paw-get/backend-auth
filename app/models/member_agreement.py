import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Identity, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MemberAgreementAction(str, enum.Enum):
    """member_agreement.action 컬럼에 대응하는 PostgreSQL ENUM(member_agreement_action)."""

    AGREE = "AGREE"
    WITHDRAW = "WITHDRAW"


class MemberAgreement(Base):
    """
    약관 동의/철회 이력 테이블.

    현재 동의 상태를 UPDATE로 덮어쓰지 않고, 동의 시 AGREE, 철회 시
    WITHDRAW 행을 새로 INSERT하여 과거 이력을 모두 보존한다.
    deleted_at은 정상적인 동의 철회에는 사용하지 않으며, 잘못 생성되거나
    무효 처리된 이력을 논리 삭제할 때만 사용한다.
    """

    __tablename__ = "member_agreement"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("member.member_id"), nullable=False
    )

    terms_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("terms.id"), nullable=False
    )

    action: Mapped[MemberAgreementAction] = mapped_column(
        Enum(MemberAgreementAction, name="member_agreement_action", native_enum=True),
        nullable=False,
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
