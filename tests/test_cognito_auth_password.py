"""
app/core/cognito_auth.py의 Phase 5 wrapper 단위 테스트
(CLIAR-157): forgot_password / confirm_forgot_password /
change_password.

boto3 예외를 wrapper가 스스로 잡지 않고 그대로 전파하는지(매핑은
service 계층 한 곳에서만 결정), backend App Client 설정이 없을 때
forgot_password/confirm_forgot_password가 RuntimeError로 실패하는지
(SECRET_HASH 계산 경유), 그리고 change_password가 ClientId/
SecretHash 없이 AccessToken만으로 호출되는지 확인한다.
"""

import pytest
from botocore.exceptions import ClientError

from app.core import cognito_auth
from app.core.cognito_auth import change_password, confirm_forgot_password, forgot_password, secret_hash
from app.core.config import settings


@pytest.fixture(autouse=True)
def backend_client_settings(monkeypatch):
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setattr(
        settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret"
    )


class _FakeClient:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def forgot_password(self, **kwargs):
        self.calls.append(("forgot_password", kwargs))
        if self.error is not None:
            raise self.error
        return {}

    def confirm_forgot_password(self, **kwargs):
        self.calls.append(("confirm_forgot_password", kwargs))
        if self.error is not None:
            raise self.error
        return {}

    def change_password(self, **kwargs):
        self.calls.append(("change_password", kwargs))
        if self.error is not None:
            raise self.error
        return {}


@pytest.fixture()
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: client)
    return client


def _client_error(code="InternalErrorException"):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Op")


class TestForgotPassword:
    def test_uses_backend_client_id_and_username(self, fake_client):
        forgot_password(email="user@example.com")

        _, kwargs = fake_client.calls[0]
        assert kwargs["ClientId"] == settings.COGNITO_BACKEND_CLIENT_ID
        assert kwargs["Username"] == "user@example.com"

    def test_secret_hash_is_computed_from_the_username(self, fake_client):
        forgot_password(email="user@example.com")

        _, kwargs = fake_client.calls[0]
        assert kwargs["SecretHash"] == secret_hash("user@example.com")

    def test_client_error_is_propagated(self, monkeypatch):
        monkeypatch.setattr(
            cognito_auth,
            "get_cognito_idp_client",
            lambda: _FakeClient(error=_client_error("UserNotFoundException")),
        )

        with pytest.raises(ClientError):
            forgot_password(email="user@example.com")

    def test_missing_backend_client_config_raises_runtime_error(
        self, monkeypatch, fake_client
    ):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)

        with pytest.raises(RuntimeError):
            forgot_password(email="user@example.com")


class TestConfirmForgotPassword:
    def test_uses_backend_client_id_username_code_and_password(self, fake_client):
        confirm_forgot_password(
            email="user@example.com",
            confirmation_code="123456",
            new_password="N3w!Passw0rd",
        )

        _, kwargs = fake_client.calls[0]
        assert kwargs["ClientId"] == settings.COGNITO_BACKEND_CLIENT_ID
        assert kwargs["Username"] == "user@example.com"
        assert kwargs["ConfirmationCode"] == "123456"
        assert kwargs["Password"] == "N3w!Passw0rd"

    def test_secret_hash_is_computed_from_the_username(self, fake_client):
        confirm_forgot_password(
            email="user@example.com",
            confirmation_code="123456",
            new_password="N3w!Passw0rd",
        )

        _, kwargs = fake_client.calls[0]
        assert kwargs["SecretHash"] == secret_hash("user@example.com")

    def test_client_error_is_propagated(self, monkeypatch):
        monkeypatch.setattr(
            cognito_auth,
            "get_cognito_idp_client",
            lambda: _FakeClient(error=_client_error("CodeMismatchException")),
        )

        with pytest.raises(ClientError):
            confirm_forgot_password(
                email="user@example.com",
                confirmation_code="123456",
                new_password="N3w!Passw0rd",
            )

    def test_missing_backend_client_config_raises_runtime_error(
        self, monkeypatch, fake_client
    ):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)

        with pytest.raises(RuntimeError):
            confirm_forgot_password(
                email="user@example.com",
                confirmation_code="123456",
                new_password="N3w!Passw0rd",
            )


class TestChangePassword:
    def test_uses_access_token_and_passwords(self, fake_client):
        change_password(
            access_token="at",
            previous_password="OldP@ss123",
            new_password="N3w!Passw0rd",
        )

        _, kwargs = fake_client.calls[0]
        assert kwargs == {
            "AccessToken": "at",
            "PreviousPassword": "OldP@ss123",
            "ProposedPassword": "N3w!Passw0rd",
        }

    def test_does_not_use_client_id_or_secret_hash(self, fake_client):
        """ChangePassword는 SECRET_HASH를 쓰지 않는다 — backend App
        Client 설정이 없어도 호출할 수 있어야 한다."""
        change_password(
            access_token="at",
            previous_password="OldP@ss123",
            new_password="N3w!Passw0rd",
        )

        _, kwargs = fake_client.calls[0]
        assert "ClientId" not in kwargs
        assert "SecretHash" not in kwargs

    def test_works_without_backend_client_config(self, monkeypatch):
        """ChangePassword는 secret_hash()를 거치지 않으므로
        COGNITO_BACKEND_CLIENT_ID/SECRET이 없어도 RuntimeError 없이
        호출된다."""
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)
        fake = _FakeClient()
        monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: fake)

        change_password(
            access_token="at", previous_password="a", new_password="b"
        )

        assert len(fake.calls) == 1

    def test_client_error_is_propagated(self, monkeypatch):
        monkeypatch.setattr(
            cognito_auth,
            "get_cognito_idp_client",
            lambda: _FakeClient(error=_client_error("NotAuthorizedException")),
        )

        with pytest.raises(ClientError):
            change_password(
                access_token="at", previous_password="a", new_password="b"
            )
