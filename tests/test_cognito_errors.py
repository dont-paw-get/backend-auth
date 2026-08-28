"""
app/core/cognito_errors.py의 Cognito 오류 -> HTTP 매핑 테스트
(CLIAR-148, Phase 1).

PLAN.md §6 매핑 표 전체를 검증한다. 이번 티켓에서는 실제 endpoint에
연결하지 않으므로, map_cognito_error_code()/map_connection_error()를
직접 호출하는 순수 단위 테스트만 작성한다.
"""

import pytest
from fastapi import status

from app.core import cognito_errors


class TestCognitoErrorMappingTable:
    @pytest.mark.parametrize(
        "error_code,expected_status,expected_detail",
        [
            ("UsernameExistsException", status.HTTP_409_CONFLICT, "이미 가입된 이메일입니다"),
            ("InvalidPasswordException", status.HTTP_400_BAD_REQUEST, "비밀번호 정책 위반"),
            ("InvalidParameterException", status.HTTP_400_BAD_REQUEST, "잘못된 요청"),
            (
                "CodeMismatchException",
                status.HTTP_400_BAD_REQUEST,
                "인증 코드가 올바르지 않습니다",
            ),
            (
                "ExpiredCodeException",
                status.HTTP_400_BAD_REQUEST,
                "인증 코드가 만료되었습니다",
            ),
            (
                "NotAuthorizedException",
                status.HTTP_401_UNAUTHORIZED,
                "이메일 또는 비밀번호가 올바르지 않습니다",
            ),
            (
                "UserNotFoundException",
                status.HTTP_401_UNAUTHORIZED,
                "이메일 또는 비밀번호가 올바르지 않습니다",
            ),
            ("TooManyRequestsException", status.HTTP_429_TOO_MANY_REQUESTS, "잠시 후 다시 시도해주세요"),
            ("LimitExceededException", status.HTTP_429_TOO_MANY_REQUESTS, "잠시 후 다시 시도해주세요"),
            (
                "TooManyFailedAttemptsException",
                status.HTTP_429_TOO_MANY_REQUESTS,
                "잠시 후 다시 시도해주세요",
            ),
        ],
    )
    def test_known_error_code_mapping(self, error_code, expected_status, expected_detail):
        http_status, detail = cognito_errors.map_cognito_error_code(error_code)

        assert http_status == expected_status
        assert detail == expected_detail

    def test_user_not_confirmed_maps_to_403_with_machine_readable_code(self):
        http_status, detail = cognito_errors.map_cognito_error_code(
            "UserNotConfirmedException"
        )

        assert http_status == status.HTTP_403_FORBIDDEN
        assert detail["code"] == "EMAIL_NOT_VERIFIED"

    def test_unknown_error_code_maps_to_502(self):
        http_status, detail = cognito_errors.map_cognito_error_code(
            "SomeUnmappedInternalException"
        )

        assert http_status == status.HTTP_502_BAD_GATEWAY
        assert detail == "인증 서비스 오류"

    def test_unknown_error_code_does_not_leak_error_code_itself(self):
        """매핑에 없는 error_code를 detail에 그대로 노출하면 Cognito
        내부 구현 정보가 새어나갈 수 있으므로, 응답에 원본 코드 문자열이
        포함되지 않아야 한다."""
        http_status, detail = cognito_errors.map_cognito_error_code(
            "SomeUnmappedInternalException"
        )

        assert "SomeUnmappedInternalException" not in str(detail)

    def test_connection_error_maps_to_502(self):
        http_status, detail = cognito_errors.map_connection_error()

        assert http_status == status.HTTP_502_BAD_GATEWAY
        assert detail == "인증 서비스 연결 실패"


class TestUserEnumerationPrevention:
    """
    핵심 보안 요구사항: NotAuthorizedException과 UserNotFoundException은
    status code와 응답 메시지가 완전히 동일해야 한다. 로그인 실패
    응답만으로 "가입된 이메일인지"를 알아낼 수 없어야 한다.
    """

    def test_not_authorized_and_user_not_found_are_identical(self):
        not_authorized = cognito_errors.map_cognito_error_code("NotAuthorizedException")
        user_not_found = cognito_errors.map_cognito_error_code("UserNotFoundException")

        assert not_authorized == user_not_found

    def test_status_codes_are_both_401(self):
        not_authorized_status, _ = cognito_errors.map_cognito_error_code(
            "NotAuthorizedException"
        )
        user_not_found_status, _ = cognito_errors.map_cognito_error_code(
            "UserNotFoundException"
        )

        assert not_authorized_status == user_not_found_status == status.HTTP_401_UNAUTHORIZED

    def test_messages_are_byte_for_byte_identical(self):
        _, not_authorized_detail = cognito_errors.map_cognito_error_code(
            "NotAuthorizedException"
        )
        _, user_not_found_detail = cognito_errors.map_cognito_error_code(
            "UserNotFoundException"
        )

        assert not_authorized_detail == user_not_found_detail
        assert isinstance(not_authorized_detail, str)
