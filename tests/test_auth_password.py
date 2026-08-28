"""
POST /api/v1/auth/password/forgot, /password/reset, /password/change
테스트 (CLIAR-157, Phase 5).

BE 주도 비밀번호 관리: backend-auth가 Cognito ForgotPassword/
ConfirmForgotPassword/ChangePassword를 직접 호출한다. 실제
AWS/Cognito에는 접속하지 않는다. app.core.cognito_auth의
get_cognito_idp_client를 monkeypatch해 boto3 client를 대체한다
(tests/test_auth_signup.py, tests/test_auth_login.py와 동일한
패턴).

forgot/reset은 아직 로그인하지 않은 사용자가 호출하는 API이므로
Bearer 인증을 요구하지 않는다. change는 Bearer Access Token이
필수이며, 인증 dependency(get_current_access_token)는
app.dependency_overrides로 override한다(tests/test_users_me.py와
동일한 패턴).
"""

import logging

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app.core import cognito_auth
from app.core.cognito_auth import secret_hash
from app.core.config import settings
from app.core.security import get_current_access_token, get_current_user_id
from app.main import app

FORGOT_ENDPOINT = "/api/v1/auth/password/forgot"
RESET_ENDPOINT = "/api/v1/auth/password/reset"
CHANGE_ENDPOINT = "/api/v1/auth/password/change"


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def backend_client_settings(monkeypatch):
    """신규 backend App Client 설정이 항상 존재하는 상태를 기본값으로
    한다(SECRET_HASH 계산이 실패하지 않도록). 실제 dev/prod의 secret
    값은 사용하지 않는다."""
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setattr(
        settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret"
    )


def _client_error(code, message="cognito message"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


def _patch_cognito(monkeypatch, fake_client):
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: fake_client)
    return fake_client


# ---------------------------------------------------------------------------
# forgot
# ---------------------------------------------------------------------------


class _FakeForgotClient:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def forgot_password(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"CodeDeliveryDetails": {"Destination": "u***@example.com"}}


class TestPasswordForgotSuccess:
    def test_registered_email_returns_204(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeForgotClient())

        response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 204

    def test_response_body_is_empty(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeForgotClient())

        response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.content == b""

    def test_calls_cognito_forgot_password_with_backend_client(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeForgotClient())

        client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert fake.calls[0]["ClientId"] == settings.COGNITO_BACKEND_CLIENT_ID
        assert fake.calls[0]["Username"] == "user@example.com"

    def test_secret_hash_is_computed_from_the_normalized_email(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeForgotClient())

        client.post(FORGOT_ENDPOINT, json={"email": "  User@Example.COM  "})

        assert fake.calls[0]["Username"] == "user@example.com"
        assert fake.calls[0]["SecretHash"] == secret_hash("user@example.com")


class TestPasswordForgotUserEnumeration:
    def test_unregistered_email_also_returns_204(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeForgotClient(error=_client_error("UserNotFoundException")),
        )

        response = client.post(
            FORGOT_ENDPOINT, json={"email": "never-registered@example.com"}
        )

        assert response.status_code == 204

    def test_registered_and_unregistered_email_responses_are_identical(
        self, client, monkeypatch
    ):
        """사용자 열거 방지: 가입 여부와 무관하게 응답이 완전히
        동일해야 한다."""
        _patch_cognito(monkeypatch, _FakeForgotClient())
        registered = client.post(
            FORGOT_ENDPOINT, json={"email": "registered@example.com"}
        )

        _patch_cognito(
            monkeypatch,
            _FakeForgotClient(error=_client_error("UserNotFoundException")),
        )
        unregistered = client.post(
            FORGOT_ENDPOINT, json={"email": "unregistered@example.com"}
        )

        assert registered.status_code == unregistered.status_code == 204
        assert registered.content == unregistered.content == b""


class TestPasswordForgotCognitoErrors:
    @pytest.mark.parametrize(
        "error_code",
        [
            "TooManyRequestsException",
            "LimitExceededException",
        ],
    )
    def test_rate_limit_errors_return_429(self, client, monkeypatch, error_code):
        _patch_cognito(
            monkeypatch, _FakeForgotClient(error=_client_error(error_code))
        )

        response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 429

    def test_unexpected_client_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeForgotClient(error=_client_error("InternalErrorException")),
        )

        response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 502

    def test_connection_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeForgotClient(
                error=EndpointConnectionError(
                    endpoint_url="https://cognito-idp.example.com"
                )
            ),
        )

        response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 502

    def test_error_response_does_not_leak_cognito_message(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeForgotClient(
                error=_client_error(
                    "InternalErrorException",
                    message="internal detail about internal-id-xyz",
                )
            ),
        )

        response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert "internal-id-xyz" not in response.text


class TestPasswordForgotValidation:
    def test_blank_email_returns_422(self, client):
        response = client.post(FORGOT_ENDPOINT, json={"email": "   "})

        assert response.status_code == 422

    def test_missing_email_returns_422(self, client):
        response = client.post(FORGOT_ENDPOINT, json={})

        assert response.status_code == 422

    def test_unexpected_field_returns_422(self, client):
        response = client.post(
            FORGOT_ENDPOINT,
            json={"email": "user@example.com", "password": "should-not-exist"},
        )

        assert response.status_code == 422

    def test_does_not_require_authorization_header(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeForgotClient())

        response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 204


class TestPasswordForgotSecrecy:
    def test_email_is_not_logged_at_error_level_beyond_generic_info(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(
            monkeypatch,
            _FakeForgotClient(error=_client_error("InternalErrorException")),
        )

        with caplog.at_level(logging.DEBUG):
            client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert settings.COGNITO_BACKEND_CLIENT_SECRET not in caplog.text


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


class _FakeResetClient:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def confirm_forgot_password(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _reset_body(**overrides):
    body = {
        "email": "user@example.com",
        "code": "123456",
        "new_password": "N3w!Passw0rd",
    }
    body.update(overrides)
    return body


class TestPasswordResetSuccess:
    def test_valid_code_returns_204(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeResetClient())

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert response.status_code == 204

    def test_calls_cognito_confirm_forgot_password_with_backend_client(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeResetClient())

        client.post(RESET_ENDPOINT, json=_reset_body())

        call = fake.calls[0]
        assert call["ClientId"] == settings.COGNITO_BACKEND_CLIENT_ID
        assert call["Username"] == "user@example.com"
        assert call["ConfirmationCode"] == "123456"
        assert call["Password"] == "N3w!Passw0rd"

    def test_secret_hash_is_computed_from_the_email(self, client, monkeypatch):
        fake = _patch_cognito(monkeypatch, _FakeResetClient())

        client.post(RESET_ENDPOINT, json=_reset_body(email="  User@Example.COM  "))

        assert fake.calls[0]["Username"] == "user@example.com"
        assert fake.calls[0]["SecretHash"] == secret_hash("user@example.com")

    def test_does_not_require_authorization_header(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeResetClient())

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert response.status_code == 204


class TestPasswordResetCognitoErrors:
    def test_code_mismatch_returns_400(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeResetClient(error=_client_error("CodeMismatchException")),
        )

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert response.status_code == 400

    def test_expired_code_returns_400(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeResetClient(error=_client_error("ExpiredCodeException")),
        )

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert response.status_code == 400

    def test_password_policy_violation_returns_400(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeResetClient(error=_client_error("InvalidPasswordException")),
        )

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert response.status_code == 400

    def test_connection_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeResetClient(
                error=EndpointConnectionError(
                    endpoint_url="https://cognito-idp.example.com"
                )
            ),
        )

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert response.status_code == 502

    def test_unexpected_client_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeResetClient(error=_client_error("InternalErrorException")),
        )

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert response.status_code == 502

    def test_error_response_does_not_leak_cognito_message(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeResetClient(
                error=_client_error(
                    "CodeMismatchException",
                    message="code mismatch for internal-id-xyz",
                )
            ),
        )

        response = client.post(RESET_ENDPOINT, json=_reset_body())

        assert "internal-id-xyz" not in response.text


class TestPasswordResetValidation:
    def test_blank_email_returns_422(self, client):
        response = client.post(RESET_ENDPOINT, json=_reset_body(email="   "))

        assert response.status_code == 422

    def test_blank_code_returns_422(self, client):
        response = client.post(RESET_ENDPOINT, json=_reset_body(code="   "))

        assert response.status_code == 422

    def test_blank_new_password_returns_422(self, client):
        response = client.post(RESET_ENDPOINT, json=_reset_body(new_password="   "))

        assert response.status_code == 422

    def test_missing_field_returns_422(self, client):
        response = client.post(
            RESET_ENDPOINT, json={"email": "user@example.com", "code": "123456"}
        )

        assert response.status_code == 422

    def test_unexpected_field_returns_422(self, client):
        response = client.post(
            RESET_ENDPOINT, json=_reset_body(member_id="attacker-supplied")
        )

        assert response.status_code == 422


class TestPasswordResetSecrecy:
    def test_new_password_is_not_echoed_in_the_response(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeResetClient())
        secret_password = "super-secret-new-password-abc123"

        response = client.post(
            RESET_ENDPOINT, json=_reset_body(new_password=secret_password)
        )

        assert secret_password not in response.text

    def test_new_password_and_code_are_not_logged(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeResetClient())
        secret_password = "super-secret-new-password-abc123"

        with caplog.at_level(logging.DEBUG):
            client.post(
                RESET_ENDPOINT,
                json=_reset_body(new_password=secret_password, code="999999"),
            )

        assert secret_password not in caplog.text
        assert "999999" not in caplog.text

    def test_validation_error_does_not_echo_password(self, client):
        secret_password = "super-secret-new-password-abc123"

        response = client.post(
            RESET_ENDPOINT,
            json={"email": "  ", "code": "123456", "new_password": secret_password},
        )

        assert response.status_code == 422
        assert secret_password not in response.text


# ---------------------------------------------------------------------------
# change
# ---------------------------------------------------------------------------


class _FakeChangeClient:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def change_password(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _change_body(**overrides):
    body = {"current_password": "OldP@ss123", "new_password": "N3w!Passw0rd"}
    body.update(overrides)
    return body


def _authorized(client, access_token="issued-access-token", sub="test-member-sub"):
    """
    /password/change는 access_token(Cognito 호출용)과 별개로
    get_current_user_id(감사 로그 member_id 용, CLIAR-160)도
    Depends()하므로 두 dependency를 함께 override해야 한다 —
    두 함수는 같은 _extract_and_verify_bearer_token을 공유하지만
    override 시에는 각각 독립적으로 대체해야 한다.
    """
    app.dependency_overrides[get_current_access_token] = lambda: access_token
    app.dependency_overrides[get_current_user_id] = lambda: sub


class TestPasswordChangeSuccess:
    def test_authorized_request_returns_204(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeChangeClient())
        _authorized(client)

        response = client.post(CHANGE_ENDPOINT, json=_change_body())

        assert response.status_code == 204

    def test_calls_cognito_change_password_with_access_token_and_passwords(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeChangeClient())
        _authorized(client, access_token="the-verified-access-token")

        client.post(
            CHANGE_ENDPOINT,
            json=_change_body(
                current_password="OldP@ss123", new_password="N3w!Passw0rd"
            ),
        )

        call = fake.calls[0]
        assert call["AccessToken"] == "the-verified-access-token"
        assert call["PreviousPassword"] == "OldP@ss123"
        assert call["ProposedPassword"] == "N3w!Passw0rd"

    def test_does_not_send_client_id_or_secret_hash(self, client, monkeypatch):
        """ChangePassword는 AccessToken만으로 인가되므로 ClientId/
        SecretHash를 보내지 않는다."""
        fake = _patch_cognito(monkeypatch, _FakeChangeClient())
        _authorized(client)

        client.post(CHANGE_ENDPOINT, json=_change_body())

        call = fake.calls[0]
        assert "ClientId" not in call
        assert "SecretHash" not in call
        assert "Username" not in call


class TestPasswordChangeAuthorization:
    def test_missing_authorization_header_returns_401(self, client, monkeypatch):
        """get_current_access_token을 override하지 않고
        Authorization 헤더 없이 요청하면 401이어야 한다(tests/
        test_users_me.py와 동일한 검증 방식)."""
        _patch_cognito(monkeypatch, _FakeChangeClient())

        response = client.post(CHANGE_ENDPOINT, json=_change_body())

        assert response.status_code == 401

    def test_missing_authorization_header_does_not_call_cognito(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeChangeClient())

        client.post(CHANGE_ENDPOINT, json=_change_body())

        assert fake.calls == []

    def test_malformed_authorization_header_returns_401(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeChangeClient())

        response = client.post(
            CHANGE_ENDPOINT,
            json=_change_body(),
            headers={"Authorization": "NotBearer abc"},
        )

        assert response.status_code == 401


class TestPasswordChangeCognitoErrors:
    def test_wrong_current_password_uses_existing_credentials_mapping(
        self, client, monkeypatch
    ):
        """기존 cognito_errors 매핑을 그대로 따른다(NotAuthorizedException
        -> 401, 동일 문구)."""
        _patch_cognito(
            monkeypatch,
            _FakeChangeClient(error=_client_error("NotAuthorizedException")),
        )
        _authorized(client)

        response = client.post(CHANGE_ENDPOINT, json=_change_body())

        assert response.status_code == 401
        assert response.json()["detail"] == "이메일 또는 비밀번호가 올바르지 않습니다"

    def test_new_password_policy_violation_returns_400(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeChangeClient(error=_client_error("InvalidPasswordException")),
        )
        _authorized(client)

        response = client.post(CHANGE_ENDPOINT, json=_change_body())

        assert response.status_code == 400

    def test_connection_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeChangeClient(
                error=EndpointConnectionError(
                    endpoint_url="https://cognito-idp.example.com"
                )
            ),
        )
        _authorized(client)

        response = client.post(CHANGE_ENDPOINT, json=_change_body())

        assert response.status_code == 502

    def test_unexpected_client_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeChangeClient(error=_client_error("InternalErrorException")),
        )
        _authorized(client)

        response = client.post(CHANGE_ENDPOINT, json=_change_body())

        assert response.status_code == 502


class TestPasswordChangeValidation:
    def test_blank_current_password_returns_422(self, client):
        _authorized(client)

        response = client.post(
            CHANGE_ENDPOINT, json=_change_body(current_password="   ")
        )

        assert response.status_code == 422

    def test_blank_new_password_returns_422(self, client):
        _authorized(client)

        response = client.post(CHANGE_ENDPOINT, json=_change_body(new_password="   "))

        assert response.status_code == 422

    def test_missing_field_returns_422(self, client):
        _authorized(client)

        response = client.post(
            CHANGE_ENDPOINT, json={"current_password": "OldP@ss123"}
        )

        assert response.status_code == 422

    def test_unexpected_field_is_rejected(self, client):
        """client가 member_id/sub/email 등을 body로 보내 인증 대상을
        스스로 결정하지 못하게 한다."""
        _authorized(client)

        response = client.post(
            CHANGE_ENDPOINT,
            json=_change_body(member_id="attacker-supplied"),
        )

        assert response.status_code == 422

    def test_email_field_is_rejected(self, client):
        _authorized(client)

        response = client.post(
            CHANGE_ENDPOINT,
            json=_change_body(email="attacker@example.com"),
        )

        assert response.status_code == 422


class TestPasswordChangeSecrecy:
    def test_passwords_are_not_echoed_in_the_response(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeChangeClient())
        _authorized(client)
        current = "super-secret-current-abc123"
        new = "super-secret-new-xyz789"

        response = client.post(
            CHANGE_ENDPOINT,
            json=_change_body(current_password=current, new_password=new),
        )

        assert current not in response.text
        assert new not in response.text

    def test_passwords_and_access_token_are_not_logged(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(monkeypatch, _FakeChangeClient())
        _authorized(client, access_token="the-verified-access-token")
        current = "super-secret-current-abc123"
        new = "super-secret-new-xyz789"

        with caplog.at_level(logging.DEBUG):
            client.post(
                CHANGE_ENDPOINT,
                json=_change_body(current_password=current, new_password=new),
            )

        assert current not in caplog.text
        assert new not in caplog.text
        assert "the-verified-access-token" not in caplog.text

    def test_failed_change_does_not_log_passwords(self, client, monkeypatch, caplog):
        _patch_cognito(
            monkeypatch,
            _FakeChangeClient(error=_client_error("NotAuthorizedException")),
        )
        _authorized(client)
        current = "super-secret-current-abc123"

        with caplog.at_level(logging.DEBUG):
            client.post(CHANGE_ENDPOINT, json=_change_body(current_password=current))

        assert current not in caplog.text
