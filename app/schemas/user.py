from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.user import MemberStatus


class MemberUpdateRequest(BaseModel):
    """
    현재 인증된 사용자의 프로필 부분 수정 요청 schema.

    CLIAR-87: agree_ai_analysis는 member 테이블 컬럼에서 제거되어
    더 이상 이 API로 수정할 수 없다.
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = None
    profile_image_url: str | None = None

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
    """

    model_config = ConfigDict(from_attributes=True)

    member_id: UUID
    email: str
    nickname: str
    profile_image_url: str | None
    status: MemberStatus
    created_at: datetime
    updated_at: datetime



class MemberBootstrapRequest(BaseModel):
    """
    Cognito 인증 완료 후 MEMBER 최초 생성 요청 schema.

    CLIAR-105: user_id/email은 더 이상 request body로 받지 않는다.
    Client가 임의의 user_id/email을 보내 서버의 identity 판단을
    좌우할 수 있는 구조는 최종 Cognito 인증 구조와 맞지 않기 때문이다.
    member_id(Cognito sub)와 email은 Authorization 헤더의 검증된
    Cognito Access Token과 Cognito GetUser 응답에서만 얻는다
    (app/api/users.py의 bootstrap_current_member 참고).

    nickname:
        서비스 nickname

    agree_terms/privacy:
        필수 동의(값 검증만 하고, 실제 이력은 member_agreement에 저장한다)

    agree_ai_analysis:
        선택 동의
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None

    agree_terms: bool
    agree_privacy: bool
    agree_ai_analysis: bool = False