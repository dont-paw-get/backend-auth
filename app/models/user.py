from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    """
    사용자 정보 테이블.

    user_id는 추후 Amazon Cognito의 `sub` 값을 그대로 사용한다.
    비밀번호는 Amazon Cognito가 관리하므로 이 테이블에는
    password/password_hash 컬럼을 두지 않는다.
    """

    __tablename__ = "member"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)

    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    nickname: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    profile_image_url: Mapped[str | None] = mapped_column(String, nullable=True)

    # 다른 서비스(도서관 서비스 등) 테이블과의 관계는 추후 정의될 예정이므로
    # 이번 작업에서는 FK 제약 없이 컬럼만 구성한다.
    representative_librarian_id: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")

    agree_terms: Mapped[bool] = mapped_column(Boolean, nullable=False)

    agree_privacy: Mapped[bool] = mapped_column(Boolean, nullable=False)

    agreed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    agree_ai_analysis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    ai_analysis_consent_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
