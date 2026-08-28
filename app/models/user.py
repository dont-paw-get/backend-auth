import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, Identity, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MemberStatus(str, enum.Enum):
    """
    member.status 컬럼에 대응하는 PostgreSQL ENUM(member_status).

    PENDING: Cognito SignUp은 완료되어 member row가 생성되었지만, 아직
        이메일 인증(ConfirmSignUp)이 끝나지 않은 상태. BE 주도 인증
        전환에서 POST /auth/signup이 이 상태로 member를 생성하고,
        POST /auth/signup/confirm이 ACTIVE로 전이시킨다.

        PENDING인 회원은 Cognito가 InitiateAuth를 거부하므로(계정
        미확인) 정상 경로에서는 access token 자체를 얻을 수 없다.
        다만 ConfirmSignUp은 성공했는데 뒤이은 DB UPDATE가 실패한
        경우(Cognito=CONFIRMED, DB=PENDING) 토큰을 가진 PENDING
        회원이 존재할 수 있으므로, get_current_member는 이 상태를
        명시적으로 차단한다(app/api/deps.py 참고).
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


class Gender(str, enum.Enum):
    """
    member.gender 컬럼에 대응하는 PostgreSQL ENUM(member_gender).

    CLIAR-120: 현재 요구사항은 MALE/FEMALE만 지원한다. OTHER/UNKNOWN
    등 다른 값은 임의로 추가하지 않는다(MemberStatus와 동일한 native
    enum 전략을 따른다).
    """

    MALE = "MALE"
    FEMALE = "FEMALE"


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

    # CLIAR-120: 기존 member row와의 호환을 위해 DB 레벨에서는 nullable을
    # 허용한다(신규 bootstrap에서의 필수 여부는 API schema 레벨에서만
    # 강제한다. app/schemas/user.py의 MemberBootstrapRequest 참고).
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    gender: Mapped[Gender | None] = mapped_column(
        Enum(Gender, name="member_gender", native_enum=True), nullable=True
    )

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
