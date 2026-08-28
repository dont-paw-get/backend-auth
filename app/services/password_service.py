"""
BE 주도 비밀번호 찾기 / 재설정 / 변경 오케스트레이션
(CLIAR-157, Phase 5, PLAN.md §4.4).

app/api/auth.py의 password/forgot, password/reset, password/change
endpoint는 request parsing과 예외 -> HTTPException 변환만 담당한다.
Cognito 호출이 뒤섞인 흐름은 모두 여기에 둔다(app/services/
signup_service.py, app/services/login_service.py와 동일한 책임
분리).

Cognito ClientError/EndpointConnectionError는 app/core/cognito_errors.py
의 단일 매핑을 통해 CognitoApiError로 변환해 전파한다(endpoint마다
error_code를 분기하지 않는다). 유일한 예외는 forgot_password의
UserNotFoundException으로, 사용자 열거 방지를 위해 이 함수 안에서
흡수하고 성공(None 반환)으로 처리한다.

이 모듈은 current_password / new_password / confirmation code /
access token / client secret 값을 로그에 남기지 않는다(PLAN.md
§9.3, §16).
"""

import logging

from app.core import cognito_auth
from app.core.cognito_errors import (
    cognito_client_error_to_exception,
    connection_error_to_exception,
)
from app.services.member_service import _normalize_email

logger = logging.getLogger(__name__)


def forgot_password(*, email: str) -> None:
    """
    POST /auth/password/forgot 오케스트레이션 (PLAN.md §4.4).

    Cognito ForgotPassword를 호출해 인증 코드를 발송한다.
    UserNotFoundException은 "가입되지 않은 이메일"임을 클라이언트에
    노출하지 않기 위해 여기서 흡수하고 성공으로 취급한다(사용자
    열거 방지). 그 외 ClientError/EndpointConnectionError는 기존
    cognito_errors 매핑을 통해 그대로 전파한다(429/502 등).
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    normalized_email = _normalize_email(email)

    try:
        cognito_auth.forgot_password(email=normalized_email)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "UserNotFoundException":
            logger.info(
                "Cognito ForgotPassword: user not found; "
                "treating as success to avoid user enumeration"
            )
            return
        logger.info("Cognito ForgotPassword rejected: error_code=%s", error_code)
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        logger.error("Cognito ForgotPassword: could not reach Cognito")
        raise connection_error_to_exception() from e


def reset_password(*, email: str, code: str, new_password: str) -> None:
    """
    POST /auth/password/reset 오케스트레이션 (PLAN.md §4.4).

    Cognito ConfirmForgotPassword를 호출한다. CodeMismatchException/
    ExpiredCodeException/InvalidPasswordException 등은 모두
    cognito_errors의 기존 매핑을 그대로 따른다(endpoint 전용 분기를
    추가하지 않는다).
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    normalized_email = _normalize_email(email)

    try:
        cognito_auth.confirm_forgot_password(
            email=normalized_email,
            confirmation_code=code,
            new_password=new_password,
        )
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        logger.info(
            "Cognito ConfirmForgotPassword rejected: error_code=%s", error_code
        )
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        logger.error("Cognito ConfirmForgotPassword: could not reach Cognito")
        raise connection_error_to_exception() from e


def change_password(
    *, access_token: str, current_password: str, new_password: str
) -> None:
    """
    POST /auth/password/change 오케스트레이션 (PLAN.md §4.4).

    인증 대상은 오직 검증된 Access Token(app/core/security.py의
    get_current_access_token)에서만 얻는다. 이 함수는 email/sub/
    member_id 등 인증 대상을 가리키는 파라미터를 받지 않는다 —
    Cognito ChangePassword API 자체가 AccessToken만으로 인가되기
    때문이다(app/core/cognito_auth.py의 change_password 참고).
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    try:
        cognito_auth.change_password(
            access_token=access_token,
            previous_password=current_password,
            new_password=new_password,
        )
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        logger.info("Cognito ChangePassword rejected: error_code=%s", error_code)
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        logger.error("Cognito ChangePassword: could not reach Cognito")
        raise connection_error_to_exception() from e
