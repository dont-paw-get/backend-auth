from datetime import date
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from app.models.user import Gender


class SignupRequest(BaseModel):
    """
    POST /api/v1/auth/signup 요청 schema (CLIAR-151).

    PLAN.md §5의 계약과, 기존 MemberBootstrapRequest(app/schemas/user.py)
    의 필드 구성을 그대로 따른다. member_id/sub는 client가 보내지
    않으며 Cognito SignUp 응답의 UserSub에서만 얻는다(bootstrap과
    동일한 원칙). CLIAR-144 최종 정책에 따라 nickname 중복 검사는
    수행하지 않는다(이 schema도, 뒤이은 service 로직도 nickname
    availability를 호출하지 않는다).

    password는 SecretStr로 선언해 repr()/로그에 평문이 노출되지
    않게 한다(실제 값이 필요한 곳에서는 get_secret_value()로 명시적
    으로 꺼내 쓴다).
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    password: SecretStr
    nickname: str | None

    birth_date: date
    gender: Gender

    agree_terms: bool
    agree_privacy: bool
    agree_ai_analysis: bool = False

    @field_validator("email")
    @classmethod
    def email_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("email must not be empty or blank")
        return value


class SignupResponse(BaseModel):
    """
    POST /api/v1/auth/signup 응답 schema (PLAN.md §5).

    status는 항상 "PENDING"이다(이메일 인증 전). code_delivery는
    Cognito SignUp 응답의 CodeDeliveryDetails를 그대로 옮긴 것으로,
    인증 코드가 어디로 발송됐는지(마스킹된 이메일)를 FE가 표시할 수
    있게 한다.
    """

    member_id: UUID
    email: str
    status: str
    code_delivery: dict | None = None


class SignupConfirmRequest(BaseModel):
    """
    POST /api/v1/auth/signup/confirm 요청 schema.

    PLAN.md §5: { "email", "code" }.
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    code: str

    @field_validator("email", "code")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or blank")
        return value


class SignupConfirmResponse(BaseModel):
    """POST /api/v1/auth/signup/confirm 응답 schema (PLAN.md §5)."""

    member_id: UUID
    email: str
    status: str


class SignupResendRequest(BaseModel):
    """
    POST /api/v1/auth/signup/resend 요청 schema.

    PLAN.md §5: { "email" }.
    """

    model_config = ConfigDict(extra="forbid")

    email: str

    @field_validator("email")
    @classmethod
    def email_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("email must not be empty or blank")
        return value


class RefreshTokenRequest(BaseModel):
    """
    POST /api/v1/auth/refresh 요청 schema (CLIAR-125).

    Access Token이 만료됐을 때 Cognito Refresh Token으로 새 Access
    Token을 재발급받기 위한 요청이다. 이 endpoint는 Bearer Access
    Token 인증을 요구하지 않는다(만료된 Access Token으로는 호출할 수
    없어야 하는 API이기 때문).
    """

    model_config = ConfigDict(extra="forbid")

    refresh_token: str

    @field_validator("refresh_token")
    @classmethod
    def refresh_token_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("refresh_token must not be empty or blank")
        return value


class RefreshTokenResponse(BaseModel):
    """
    POST /api/v1/auth/refresh 응답 schema.

    Refresh Token Rotation이 비활성화되어 있으므로 새 refresh_token은
    포함하지 않는다(기존 refresh token을 원래 만료 시점까지 계속
    사용한다). id_token은 Cognito 응답에 포함되어 있으면 함께
    반환한다(선택 필드).
    """

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    id_token: str | None = None


class AvailabilityField(str, Enum):
    """중복 확인을 지원하는 필드 목록."""

    EMAIL = "EMAIL"
    NICKNAME = "NICKNAME"


class AvailabilityRequest(BaseModel):
    field: AvailabilityField
    value: str

    @field_validator("value")
    @classmethod
    def value_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty or blank")
        return value


class AvailabilityResponse(BaseModel):
    field: AvailabilityField
    available: bool
