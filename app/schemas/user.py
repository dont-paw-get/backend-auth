from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class MemberUpdateRequest(BaseModel):
    """
    현재 인증된 사용자의 프로필 부분 수정 요청 schema.
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = None
    profile_image_url: str | None = None
    agree_ai_analysis: bool | None = None

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
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    email: str
    nickname: str
    profile_image_url: str | None
    status: str
    agree_ai_analysis: bool
    created_at: datetime
    updated_at: datetime



class MemberBootstrapRequest(BaseModel):
    """
    Cognito 인증 완료 후 MEMBER 최초 생성 요청 schema.

    user_id:
        Cognito sub 값 저장

    email:
        Cognito 인증 email

    nickname:
        서비스 nickname

    agree_terms/privacy:
        필수 동의

    agree_ai_analysis:
        선택 동의
    """

    user_id: str
    email: str
    nickname: str | None

    agree_terms: bool
    agree_privacy: bool
    agree_ai_analysis: bool = False