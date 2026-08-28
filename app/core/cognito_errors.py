"""
Cognito 예외 -> HTTP 응답 매핑 단일 위치 (CLIAR-148, Phase 1).

PLAN.md의 "Cognito 오류 -> HTTP 매핑" 표를 코드로 옮긴 것이다. 앞으로
Phase 3/4/5의 signup/login/refresh/password endpoint들이 각자
if/elif로 error_code를 분기하지 않고, 이 모듈의 매핑 하나를 공유하게
하기 위한 준비 단계다. 이번 티켓에서는 실제 endpoint에 연결하지
않는다(순수 매핑 함수 + 테스트까지만).

핵심 보안 요구사항: NotAuthorizedException과 UserNotFoundException은
status code와 응답 메시지가 완전히 동일해야 한다(사용자 존재 여부를
응답 차이로 추측할 수 없게 함, user enumeration 방지). 아래
_CREDENTIALS_ERROR 상수를 두 error_code가 그대로 공유하는 방식으로
이를 보장한다(같은 튜플 객체를 참조하므로 둘 중 하나만 수정하고 다른
쪽을 깜빡하는 실수가 원천적으로 발생할 수 없다).
"""

from fastapi import status

# (HTTP status code, detail) 형태로 통일한다. detail이 dict인 경우
# FE가 기계적으로 분기할 수 있는 code 필드를 포함한다(예:
# EMAIL_NOT_VERIFIED). 나머지는 사람이 읽는 메시지 문자열이다.
ErrorResponse = tuple[int, str | dict]

# NotAuthorizedException과 UserNotFoundException이 공유하는 응답.
# 로그인 실패 시 "아이디가 없다"와 "비밀번호가 틀렸다"를 구분해서
# 응답하면 공격자가 가입된 이메일을 알아낼 수 있으므로(user
# enumeration), 두 경우 모두 이 동일한 문구를 반환해야 한다.
_INVALID_CREDENTIALS: ErrorResponse = (
    status.HTTP_401_UNAUTHORIZED,
    "이메일 또는 비밀번호가 올바르지 않습니다",
)

_TOO_MANY_REQUESTS: ErrorResponse = (
    status.HTTP_429_TOO_MANY_REQUESTS,
    "잠시 후 다시 시도해주세요",
)

# Cognito ClientError의 Error.Code -> (status, detail) 매핑.
COGNITO_ERROR_MAPPING: dict[str, ErrorResponse] = {
    "UsernameExistsException": (
        status.HTTP_409_CONFLICT,
        "이미 가입된 이메일입니다",
    ),
    "InvalidPasswordException": (
        status.HTTP_400_BAD_REQUEST,
        "비밀번호 정책 위반",
    ),
    "InvalidParameterException": (
        status.HTTP_400_BAD_REQUEST,
        "잘못된 요청",
    ),
    "CodeMismatchException": (
        status.HTTP_400_BAD_REQUEST,
        "인증 코드가 올바르지 않습니다",
    ),
    "ExpiredCodeException": (
        status.HTTP_400_BAD_REQUEST,
        "인증 코드가 만료되었습니다",
    ),
    "UserNotConfirmedException": (
        status.HTTP_403_FORBIDDEN,
        {
            "code": "EMAIL_NOT_VERIFIED",
            "message": "Email verification has not been completed",
        },
    ),
    "NotAuthorizedException": _INVALID_CREDENTIALS,
    "UserNotFoundException": _INVALID_CREDENTIALS,
    "TooManyRequestsException": _TOO_MANY_REQUESTS,
    "LimitExceededException": _TOO_MANY_REQUESTS,
    "TooManyFailedAttemptsException": _TOO_MANY_REQUESTS,
}

# 매핑 표에 없는 그 외 ClientError(서비스 내부 오류 등).
DEFAULT_CLIENT_ERROR: ErrorResponse = (
    status.HTTP_502_BAD_GATEWAY,
    "인증 서비스 오류",
)

# botocore.exceptions.EndpointConnectionError 등 네트워크 자체가
# 실패한 경우(ClientError가 아니라 Cognito에 아예 도달하지 못한 경우).
CONNECTION_ERROR: ErrorResponse = (
    status.HTTP_502_BAD_GATEWAY,
    "인증 서비스 연결 실패",
)


def map_cognito_error_code(error_code: str) -> ErrorResponse:
    """
    boto3 ClientError의 e.response["Error"]["Code"] 값을
    (HTTP status, detail) 튜플로 변환한다.

    매핑 표에 없는 error_code(예상 밖의 Cognito 오류, 서비스 장애 등)는
    DEFAULT_CLIENT_ERROR(502)로 취급한다. Cognito의 내부 오류 메시지를
    그대로 노출하지 않기 위해, 이 함수는 error_code 자체를 응답에
    포함하지 않는다(로깅은 호출자의 책임).
    """
    return COGNITO_ERROR_MAPPING.get(error_code, DEFAULT_CLIENT_ERROR)


def map_connection_error() -> ErrorResponse:
    """네트워크 자체가 실패한 경우(EndpointConnectionError 등)의 응답."""
    return CONNECTION_ERROR


class CognitoApiError(Exception):
    """
    Cognito boto3 ClientError/EndpointConnectionError를 이미
    map_cognito_error_code()/map_connection_error()로 변환한 (status,
    detail)을 담아 상위 계층(API router)에 전달하기 위한 wrapper
    (CLIAR-151, Phase 3).

    service 계층(예: signup_service.py)이 이 예외를 던지면, router는
    e.status_code/e.detail을 그대로 HTTPException에 옮기기만 하면
    되므로, 여러 endpoint/service가 각자 error_code를 if/elif로
    분기하는 중복을 피할 수 있다.
    """

    def __init__(self, status_code: int, detail: str | dict):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail if isinstance(detail, str) else str(detail))


def cognito_client_error_to_exception(error_code: str) -> CognitoApiError:
    """boto3 ClientError의 error_code를 CognitoApiError로 변환한다."""
    status_code, detail = map_cognito_error_code(error_code)
    return CognitoApiError(status_code, detail)


def connection_error_to_exception() -> CognitoApiError:
    """EndpointConnectionError 등 네트워크 실패를 CognitoApiError로 변환한다."""
    status_code, detail = map_connection_error()
    return CognitoApiError(status_code, detail)
