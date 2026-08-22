from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class MemberUpdateRequest(BaseModel):
    """
    현재 인증된 사용자(MEMBER)의 프로필 부분 수정(PATCH) 요청 schema.

    PATCH이므로 모든 필드는 optional이며, 요청에 실제로 포함된 필드만
    수정 대상으로 취급한다(app/api/users.py에서 model_dump(exclude_unset=True)
    로 구분). user_id/email/status 등 수정 금지 필드는 이 schema에
    존재하지 않으며, extra="forbid"로 요청 body에 허용되지 않은 필드가
    포함되면 422로 명확히 거부한다.

    nickname은 "optional(생략 가능)"이지만 "nullable(null 저장 가능)"은
    아니다. DB의 member.nickname 컬럼이 NOT NULL이므로, 필드를 생략하면
    기존 값을 유지하되(exclude_unset으로 구분) 필드를 null로 명시하면
    스키마 단계에서 422로 거부한다. 빈 문자열/공백 문자열도 같은 이유로
    거부하고, 정상 값은 strip하여 정규화한다.

    profile_image_url은 nullable 컬럼이므로 null을 명시하면 프로필
    이미지 제거로 정상 처리한다 (validator 없음).
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = None
    profile_image_url: str | None = None
    agree_ai_analysis: bool | None = None

    @field_validator("nickname")
    @classmethod
    def nickname_must_be_non_null_and_non_blank(cls, value: str | None) -> str:
        """
        기본값(None)은 "필드 생략"을 표현하기 위한 것일 뿐이며, Pydantic은
        필드가 요청에 없을 때(exclude_unset 대상)는 이 validator를 아예
        실행하지 않는다(validate_default=False가 기본값). 즉 이 validator가
        실제로 호출되는 시점은 클라이언트가 "nickname" 키를 요청 body에
        명시적으로 포함한 경우뿐이며, 그 값이 null이면 여기서 즉시
        422로 거부한다.
        """
        if value is None:
            raise ValueError("nickname must not be null")

        normalized = value.strip()
        if not normalized:
            raise ValueError("nickname must not be empty or blank")
        return normalized


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
