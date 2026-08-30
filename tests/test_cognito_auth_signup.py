"""
app/core/cognito_auth.py의 Phase 3 wrapper(sign_up/confirm_sign_up/
resend_confirmation_code/admin_get_user/admin_delete_user) 테스트
(CLIAR-151).

실제 AWS/Cognito에 접속하지 않기 위해 get_cognito_idp_client를
monkeypatch한다.
"""

import pytest

from app.core import cognito_auth
from app.core.config import settings


@pytest.fixture()
def backend_client_settings(monkeypatch):
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret")
    return settings


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: fake_client)


class TestSignUp:
    def test_calls_sign_up_with_backend_client_id(
        self, monkeypatch, backend_client_settings
    ):
        received = {}

        class _FakeClient:
            def sign_up(self, **kwargs):
                received.update(kwargs)
                return {"UserSub": "sub-0001", "UserConfirmed": False}

        _patch_client(monkeypatch, _FakeClient())

        result = cognito_auth.sign_up(email="user@example.com", password="P@ssw0rd!")

        assert received["ClientId"] == "backend-client-id"
        assert result["UserSub"] == "sub-0001"

    def test_username_is_email(self, monkeypatch, backend_client_settings):
        received = {}

        class _FakeClient:
            def sign_up(self, **kwargs):
                received.update(kwargs)
                return {"UserSub": "sub-0001"}

        _patch_client(monkeypatch, _FakeClient())

        cognito_auth.sign_up(email="user@example.com", password="P@ssw0rd!")

        assert received["Username"] == "user@example.com"

    def test_includes_secret_hash(self, monkeypatch, backend_client_settings):
        received = {}

        class _FakeClient:
            def sign_up(self, **kwargs):
                received.update(kwargs)
                return {"UserSub": "sub-0001"}

        _patch_client(monkeypatch, _FakeClient())

        cognito_auth.sign_up(email="user@example.com", password="P@ssw0rd!")

        expected_hash = cognito_auth.secret_hash("user@example.com")
        assert received["SecretHash"] == expected_hash

    def test_includes_email_user_attribute(self, monkeypatch, backend_client_settings):
        received = {}

        class _FakeClient:
            def sign_up(self, **kwargs):
                received.update(kwargs)
                return {"UserSub": "sub-0001"}

        _patch_client(monkeypatch, _FakeClient())

        cognito_auth.sign_up(email="user@example.com", password="P@ssw0rd!")

        assert {"Name": "email", "Value": "user@example.com"} in received["UserAttributes"]

    def test_client_error_propagates_uncaught(self, monkeypatch, backend_client_settings):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "UsernameExistsException", "Message": "exists"}},
                    "SignUp",
                )

        _patch_client(monkeypatch, _FakeClient())

        with pytest.raises(ClientError):
            cognito_auth.sign_up(email="user@example.com", password="P@ssw0rd!")


class TestConfirmSignUp:
    def test_calls_confirm_sign_up_with_code(self, monkeypatch, backend_client_settings):
        received = {}

        class _FakeClient:
            def confirm_sign_up(self, **kwargs):
                received.update(kwargs)

        _patch_client(monkeypatch, _FakeClient())

        cognito_auth.confirm_sign_up(email="user@example.com", confirmation_code="123456")

        assert received["Username"] == "user@example.com"
        assert received["ConfirmationCode"] == "123456"
        assert received["ClientId"] == "backend-client-id"

    def test_client_error_propagates_uncaught(self, monkeypatch, backend_client_settings):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def confirm_sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "CodeMismatchException", "Message": "bad code"}},
                    "ConfirmSignUp",
                )

        _patch_client(monkeypatch, _FakeClient())

        with pytest.raises(ClientError):
            cognito_auth.confirm_sign_up(email="user@example.com", confirmation_code="000000")


class TestResendConfirmationCode:
    def test_calls_resend_with_backend_client(self, monkeypatch, backend_client_settings):
        received = {}

        class _FakeClient:
            def resend_confirmation_code(self, **kwargs):
                received.update(kwargs)
                return {"CodeDeliveryDetails": {"Destination": "u***@example.com"}}

        _patch_client(monkeypatch, _FakeClient())

        result = cognito_auth.resend_confirmation_code(email="user@example.com")

        assert received["Username"] == "user@example.com"
        assert result["CodeDeliveryDetails"]["Destination"] == "u***@example.com"


class TestAdminGetUser:
    def test_calls_admin_get_user_with_user_pool_id(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", "test-pool-id")
        received = {}

        class _FakeClient:
            def admin_get_user(self, **kwargs):
                received.update(kwargs)
                return {"UserAttributes": [{"Name": "sub", "Value": "sub-0001"}]}

        _patch_client(monkeypatch, _FakeClient())

        result = cognito_auth.admin_get_user(email="user@example.com")

        assert received["UserPoolId"] == "test-pool-id"
        assert received["Username"] == "user@example.com"
        assert result["UserAttributes"][0]["Value"] == "sub-0001"

    def test_does_not_require_backend_client_secret(self, monkeypatch):
        """admin API는 IAM으로 인가되며 App Client secret과 무관하므로,
        COGNITO_BACKEND_CLIENT_SECRET이 없어도 호출 가능해야 한다."""
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)

        class _FakeClient:
            def admin_get_user(self, **kwargs):
                return {"UserAttributes": []}

        _patch_client(monkeypatch, _FakeClient())

        # RuntimeError(secret_hash 관련)가 발생하지 않아야 한다.
        cognito_auth.admin_get_user(email="user@example.com")


class TestAdminDeleteUser:
    def test_calls_admin_delete_user_with_user_pool_id(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", "test-pool-id")
        received = {}

        class _FakeClient:
            def admin_delete_user(self, **kwargs):
                received.update(kwargs)

        _patch_client(monkeypatch, _FakeClient())

        cognito_auth.admin_delete_user(email="user@example.com")

        assert received["UserPoolId"] == "test-pool-id"
        assert received["Username"] == "user@example.com"

    def test_failure_propagates_uncaught(self, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def admin_delete_user(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                    "AdminDeleteUser",
                )

        _patch_client(monkeypatch, _FakeClient())

        with pytest.raises(ClientError):
            cognito_auth.admin_delete_user(email="user@example.com")


class TestExtractSubFromAdminGetUser:
    def test_extracts_sub_value(self):
        response = {
            "UserAttributes": [
                {"Name": "email", "Value": "user@example.com"},
                {"Name": "sub", "Value": "sub-0001"},
            ]
        }

        assert cognito_auth.extract_sub_from_admin_get_user(response) == "sub-0001"

    def test_missing_sub_raises_value_error(self):
        response = {"UserAttributes": [{"Name": "email", "Value": "user@example.com"}]}

        with pytest.raises(ValueError):
            cognito_auth.extract_sub_from_admin_get_user(response)
