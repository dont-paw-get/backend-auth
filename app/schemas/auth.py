from enum import Enum

from pydantic import BaseModel, field_validator


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
