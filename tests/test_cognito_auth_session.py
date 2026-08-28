"""
app/core/cognito_auth.py의 Phase 4 wrapper 단위 테스트
(CLIAR-153): initiate_password_auth / refresh_auth /
revoke_refresh_token / get_user_sub.

boto3 예외를 wrapper가 스스로 잡지 않고 그대로 전파하는지(매핑은
service 계층 한 곳에서만 결정), 그리고 backend App Client 설정이
없을 때 조용히 잘못된 값으로 호출하지 않고 RuntimeError로 실패하는지
확인한다(Phase 1 secret_hash와 동일한 정책).
"""

import pytest
from botocore.exceptions import ClientError

from app.core import cognito_auth
from app.core.cognito_auth import (
    extract_sub_from_admin_get_user,
    extract_sub_from_user_attributes,
    get_user_sub,
    initiate_password_auth,
    refresh_auth,
    revoke_refresh_token,
    secret_hash,
)
from app.core.config import settings

SUB = "11111111-2222-3333-4444-555555555555"


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

    def initiate_auth(self, **kwargs):
        self.calls.append(("initiate_auth", kwargs))
        if self.error is not None:
            raise self.error
        return {"AuthenticationResult": {"AccessToken": "a"}}

    def revoke_token(self, **kwargs):
        self.calls.append(("revoke_token", kwargs))
        if self.error is not None:
            raise self.error
        return {}

    def get_user(self, AccessToken):
        self.calls.append(("get_user", {"AccessToken": AccessToken}))
        if self.error is not None:
            raise self.error
        return {"UserAttributes": [{"Name": "sub", "Value": SUB}]}


@pytest.fixture()
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: client)
    return client


def _client_error(code="InternalErrorException"):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Op")


class TestInitiatePasswordAuth:
    def test_uses_user_password_auth_flow_and_backend_client(self, fake_client):
        initiate_password_auth(email="user@example.com", password="pw")

        _, kwargs = fake_client.calls[0]
        assert kwargs["AuthFlow"] == "USER_PASSWORD_AUTH"
        assert kwargs["ClientId"] == settings.COGNITO_BACKEND_CLIENT_ID

    def test_secret_hash_is_computed_from_the_username(self, fake_client):
        initiate_password_auth(email="user@example.com", password="pw")

        _, kwargs = fake_client.calls[0]
        assert kwargs["AuthParameters"]["USERNAME"] == "user@example.com"
        assert kwargs["AuthParameters"]["SECRET_HASH"] == secret_hash(
            "user@example.com"
        )

    def test_client_error_is_propagated(self, monkeypatch):
        monkeypatch.setattr(
            cognito_auth,
            "get_cognito_idp_client",
            lambda: _FakeClient(error=_client_error("NotAuthorizedException")),
        )

        with pytest.raises(ClientError):
            initiate_password_auth(email="user@example.com", password="pw")

    def test_missing_backend_client_config_raises_runtime_error(
        self, monkeypatch, fake_client
    ):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)

        with pytest.raises(RuntimeError):
            initiate_password_auth(email="user@example.com", password="pw")


class TestRefreshAuth:
    def test_uses_refresh_token_auth_flow_and_backend_client(self, fake_client):
        refresh_auth(refresh_token="rt", sub=SUB)

        _, kwargs = fake_client.calls[0]
        assert kwargs["AuthFlow"] == "REFRESH_TOKEN_AUTH"
        assert kwargs["ClientId"] == settings.COGNITO_BACKEND_CLIENT_ID
        assert kwargs["AuthParameters"]["REFRESH_TOKEN"] == "rt"

    def test_secret_hash_is_computed_from_the_sub_not_the_token(self, fake_client):
        refresh_auth(refresh_token="rt", sub=SUB)

        _, kwargs = fake_client.calls[0]
        assert kwargs["AuthParameters"]["SECRET_HASH"] == secret_hash(SUB)
        assert kwargs["AuthParameters"]["SECRET_HASH"] != secret_hash("rt")

    def test_missing_backend_client_config_raises_runtime_error(
        self, monkeypatch, fake_client
    ):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)

        with pytest.raises(RuntimeError):
            refresh_auth(refresh_token="rt", sub=SUB)


class TestRevokeRefreshToken:
    def test_passes_client_secret_itself(self, fake_client):
        """RevokeToken은 SECRET_HASH가 아니라 ClientSecret을 받는다."""
        revoke_refresh_token(refresh_token="rt")

        _, kwargs = fake_client.calls[0]
        assert kwargs == {
            "Token": "rt",
            "ClientId": settings.COGNITO_BACKEND_CLIENT_ID,
            "ClientSecret": settings.COGNITO_BACKEND_CLIENT_SECRET,
        }

    def test_client_error_is_propagated(self, monkeypatch):
        monkeypatch.setattr(
            cognito_auth,
            "get_cognito_idp_client",
            lambda: _FakeClient(error=_client_error()),
        )

        with pytest.raises(ClientError):
            revoke_refresh_token(refresh_token="rt")

    def test_missing_backend_client_config_raises_runtime_error(
        self, monkeypatch, fake_client
    ):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)

        with pytest.raises(RuntimeError):
            revoke_refresh_token(refresh_token="rt")


class TestGetUserSub:
    def test_returns_sub_from_get_user_response(self, fake_client):
        assert get_user_sub(access_token="at") == SUB
        assert fake_client.calls[0] == ("get_user", {"AccessToken": "at"})

    def test_client_error_is_propagated(self, monkeypatch):
        monkeypatch.setattr(
            cognito_auth,
            "get_cognito_idp_client",
            lambda: _FakeClient(error=_client_error()),
        )

        with pytest.raises(ClientError):
            get_user_sub(access_token="at")

    def test_missing_sub_attribute_raises_value_error(self, monkeypatch):
        class _NoSubClient:
            def get_user(self, AccessToken):
                return {"UserAttributes": [{"Name": "email", "Value": "a@b.c"}]}

        monkeypatch.setattr(
            cognito_auth, "get_cognito_idp_client", lambda: _NoSubClient()
        )

        with pytest.raises(ValueError):
            get_user_sub(access_token="at")


class TestSubExtractionIsShared:
    def test_admin_alias_delegates_to_the_shared_helper(self):
        response = {"UserAttributes": [{"Name": "sub", "Value": SUB}]}

        assert extract_sub_from_admin_get_user(response) == SUB
        assert extract_sub_from_user_attributes(response) == SUB
