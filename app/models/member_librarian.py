import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Integer, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MemberLibrarian(Base):
    """
    회원이 보유한 사서(librarian) 인스턴스 테이블.

    같은 librarian_id의 사서를 여러 인스턴스 보유할 수 있으므로
    (member_id, librarian_id) UNIQUE는 두지 않는다. librarian_id는
    Librarian 서비스가 소유하는 식별자이므로 FK를 걸지 않는다.
    """

    __tablename__ = "member_librarian"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("member.member_id"), nullable=False
    )

    librarian_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    evolution_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    is_representative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    acquired_at: Mapped[datetime] = mapped_column(
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
