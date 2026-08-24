import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Identity, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MemberStatus(str, enum.Enum):
    """member.status 컬럼에 대응하는 PostgreSQL ENUM(member_status)."""

    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


class User(Base):
    """
    사용자 정보 테이블(실제 테이블명: member).

    member_id는 Amazon Cognito의 `sub` 값을 UUID로 저장한다.
    인증(회원가입/로그인/비밀번호)은 Amazon Cognito가 담당하므로
    이 테이블에는 password/password_hash 컬럼을 두지 않는다.

    약관 동의/철회 이력은 이 테이블이 아니라 terms + member_agreement에서
    관리한다(CLIAR-87). 대표 사서 여부는 member_librarian.is_representative
    에서 관리하므로 이 테이블에는 관련 컬럼을 두지 않는다.
    """

    __tablename__ = "member"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, unique=True
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    nickname: Mapped[str] = mapped_column(String(255), nullable=False)

    profile_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[MemberStatus] = mapped_column(
        Enum(MemberStatus, name="member_status", native_enum=True), nullable=False
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
