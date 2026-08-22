from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MemberResponse(BaseModel):
    """
    현재 인증된 사용자(MEMBER)의 조회 응답 schema.

    비밀번호 관련 필드는 존재하지 않는다 (Cognito가 비밀번호를 관리).
    agree_terms/agree_privacy/agreed_at은 이번 API 응답 범위가 아니므로
    포함하지 않는다.
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
