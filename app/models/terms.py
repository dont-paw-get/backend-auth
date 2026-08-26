from datetime import datetime

from sqlalchemy import Boolean, DateTime, Identity, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Terms(Base):
    """
    약관 원문 테이블.

    약관 내용이 변경되면 기존 행을 덮어쓰지 않고 새 행을 INSERT하여
    과거 약관 내용을 보존한다(별도의 version 컬럼은 사용하지 않는다).
    같은 code에 대해 현재 유효한(만료/삭제되지 않은) 행은 최대 1개만
    존재해야 하며, 이는 Alembic migration에서 생성하는 partial unique
    index(uk_terms_active_code)로 보장한다.
    """

    __tablename__ = "terms"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)

    code: Mapped[str] = mapped_column(String(50), nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
