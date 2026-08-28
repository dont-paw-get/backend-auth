from app.core.cognito import refresh_cognito_access_token
from app.repositories.user_repository import UserRepository
from app.schemas.auth import AvailabilityField, AvailabilityResponse, RefreshTokenResponse


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


def refresh_access_token(refresh_token: str) -> RefreshTokenResponse:
    """
    Cognito Refresh Token으로 새 Access Token을 재발급받는다
    (CLIAR-125).

    실제 Cognito 호출은 app.core.cognito.refresh_cognito_access_token이
    담당하며, 이 함수는 그 결과를 API 응답 schema로 변환하는 역할만
    한다. ValueError(잘못된/만료된 refresh token)와 RuntimeError
    (Cognito 호출 실패)는 그대로 호출자(router)에게 전달되어 각각
    401/502로 매핑된다.
    """
    auth_result = refresh_cognito_access_token(refresh_token)

    return RefreshTokenResponse(
        access_token=auth_result["AccessToken"],
        expires_in=auth_result["ExpiresIn"],
        id_token=auth_result.get("IdToken"),
    )
