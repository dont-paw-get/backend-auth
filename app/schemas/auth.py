from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator


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
