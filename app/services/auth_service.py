from app.repositories.user_repository import UserRepository
from app.schemas.auth import AvailabilityField, AvailabilityResponse


class UnsupportedAvailabilityFieldError(ValueError):
    """지원하지 않는 field 값이 요청된 경우 발생한다."""


def check_availability(
    field: AvailabilityField, value: str, user_repository: UserRepository
) -> AvailabilityResponse:
    """
    이메일/닉네임의 사용 가능 여부를 확인한다.

    - EMAIL: 앞뒤 공백 제거 후 소문자로 정규화하여 조회한다.
    - NICKNAME: 앞뒤 공백만 제거하고, 대소문자는 그대로 조회한다.

    이 함수는 조회만 수행하며 사용자를 생성/수정하지 않는다.
    """
    if field == AvailabilityField.EMAIL:
        normalized_value = value.strip().lower()
        exists = user_repository.exists_by_email(normalized_value)
    elif field == AvailabilityField.NICKNAME:
        normalized_value = value.strip()
        exists = user_repository.exists_by_nickname(normalized_value)
    else:  # pragma: no cover - AvailabilityField가 아닌 값은 스키마에서 이미 차단됨
        raise UnsupportedAvailabilityFieldError(f"Unsupported field: {field}")

    return AvailabilityResponse(field=field, available=not exists)
