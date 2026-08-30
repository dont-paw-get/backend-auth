from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.user import Gender, MemberStatus


class MemberUpdateRequest(BaseModel):
    """
    현재 인증된 사용자의 프로필 부분 수정 요청 schema.

    CLIAR-87: agree_ai_analysis는 member 테이블 컬럼에서 제거되어
    더 이상 이 API로 수정할 수 없다.

    CLIAR-120: birth_date/gender도 부분 수정 가능하다. 둘 다 optional이며
    보내지 않은 필드는 기존 값을 유지한다(model_dump(exclude_unset=True)
    를 사용하는 기존 PATCH 흐름, app/api/users.py 참고).
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = None
    profile_image_url: str | None = None
    birth_date: date | None = None
    gender: Gender | None = None

    @field_validator("nickname")
    @classmethod
    def nickname_must_be_non_null_and_non_blank(
        cls, 
        value: str | None
    ) -> str:
        if value is None:
            raise ValueError("nickname must not be null")

        normalized = value.strip()

        if not normalized:
            raise ValueError("nickname must not be empty or blank")

        return normalized



class MemberResponse(BaseModel):
    """
    MEMBER 조회 응답 schema.

    CLIAR-87: user_id(str) -> member_id(UUID)로 변경. 약관 동의 관련
    필드(agree_ai_analysis 등)는 member 테이블에서 제거되어(terms +
    member_agreement로 이관) 더 이상 이 응답에 포함하지 않는다.

    CLIAR-120: birth_date/gender를 응답에 포함한다. DB 컬럼이
    nullable이므로(기존 row 호환), 두 필드 모두 Optional로 선언해
    기존 member(NULL 값)를 조회해도 serialization 오류 없이 null로
    응답한다.
    """

    model_config = ConfigDict(from_attributes=True)

    member_id: UUID
    email: str
    nickname: str
    profile_image_url: str | None
    birth_date: date | None
    gender: Gender | None
    status: MemberStatus
    created_at: datetime
    updated_at: datetime