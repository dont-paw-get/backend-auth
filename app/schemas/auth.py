from datetime import date
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from app.models.user import Gender
from app.schemas.user import MemberResponse


class SignupRequest(BaseModel):
    """
    POST /api/v1/auth/signup 요청 schema (CLIAR-151).

    PLAN.md §5의 계약을 따른다. member_id/sub는 client가 보내지
    않으며 Cognito SignUp 응답의 UserSub에서만 얻는다. CLIAR-144
    최종 정책에 따라 nickname 중복 검사는 수행하지 않는다(이
    schema도, 뒤이은 service 로직도 nickname availability를 호출하지
    않는다).

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


class LoginRequest(BaseModel):
    """
    POST /api/v1/auth/login 요청 schema (CLIAR-153, PLAN.md §5).

    password는 SecretStr로 선언해 repr()/로그/예외 메시지에 평문이
    노출되지 않게 한다(SignupRequest와 동일한 정책, PLAN.md §9.3).
    실제 값이 필요한 곳에서만 get_secret_value()로 명시적으로 꺼낸다.
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    password: SecretStr

    @field_validator("email")
    @classmethod
    def email_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("email must not be empty or blank")
        return value

    @field_validator("password")
    @classmethod
    def password_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            # 예외 메시지에 입력값 자체를 포함하지 않는다(PLAN.md §9.3).
            raise ValueError("password must not be empty or blank")
        return value


class LoginResponse(BaseModel):
    """
    POST /api/v1/auth/login 응답 schema (PLAN.md §4.2, §5).

    Refresh Token은 이 응답 body에 포함하지 않는다. XSS로 탈취되지
    않도록 HttpOnly 쿠키(refresh_token)로만 전달하기 때문이다(D3).
    Cognito sub 역시 body가 아니라 refresh_sub HttpOnly 쿠키로만
    전달한다.

    id_token은 Cognito 응답에 포함되어 있으면 함께 반환한다
    (RefreshTokenResponse와 동일하게 선택 필드로 둔다).
    """

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    id_token: str | None = None
    member: MemberResponse


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


class PasswordForgotRequest(BaseModel):
    """
    POST /api/v1/auth/password/forgot 요청 schema (CLIAR-157,
    PLAN.md §4.4).

    가입 여부와 무관하게 항상 204를 반환해야 하므로(사용자 열거
    방지), 이 schema 자체는 password 계열 필드를 갖지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    email: str

    @field_validator("email")
    @classmethod
    def email_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("email must not be empty or blank")
        return value


class PasswordResetRequest(BaseModel):
    """
    POST /api/v1/auth/password/reset 요청 schema (CLIAR-157,
    PLAN.md §4.4).

    new_password는 SecretStr로 선언해 repr()/로그/예외 메시지에
    평문이 노출되지 않게 한다(LoginRequest/SignupRequest와 동일한
    정책).
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    code: str
    new_password: SecretStr

    @field_validator("email", "code")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or blank")
        return value

    @field_validator("new_password")
    @classmethod
    def new_password_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("new_password must not be empty or blank")
        return value


class PasswordChangeRequest(BaseModel):
    """
    POST /api/v1/auth/password/change 요청 schema (CLIAR-157,
    PLAN.md §4.4).

    member_id/sub/email 등 인증 대상을 가리키는 필드는 이 schema에
    두지 않는다 — 인증 대상은 항상 Bearer Access Token에서만
    얻는다(app/core/security.py의 get_current_access_token).
    current_password/new_password 모두 SecretStr로 선언한다.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr
    new_password: SecretStr

    @field_validator("current_password", "new_password")
    @classmethod
    def passwords_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("password must not be empty or blank")
        return value
