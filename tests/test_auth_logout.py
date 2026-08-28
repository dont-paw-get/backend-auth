"""
POST /api/v1/auth/logout 테스트 (CLIAR-153, Phase 4).

request body 없음. refresh_token 쿠키가 있으면 Cognito RevokeToken을
호출하고, 성공/실패와 무관하게 로컬 쿠키를 삭제한 뒤 204를 반환한다
(사용자 관점에서 멱등).

실제 AWS/Cognito에는 접속하지 않는다.
"""

import logging

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app.core import cognito_auth
from app.core.config import settings
from app.main import app

ENDPOINT = "/api/v1/auth/logout"

REFRESH_TOKEN = "stored-refresh-token"
REFRESH_SUB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def backend_client_settings(monkeypatch):
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setattr(
        settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret"
    )


class _FakeCognitoClient:
    def __init__(self, *, error=None):
        self.error = error
        self.revoke_calls = []

    def revoke_token(self, **kwargs):
        self.revoke_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _patch_cognito(monkeypatch, fake_client):
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: fake_client)
    return fake_client


def _client_error(code, message="cognito message"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "RevokeToken")


def _set_refresh_cookies(client, *, refresh_token=REFRESH_TOKEN, sub=REFRESH_SUB):
    client.cookies.clear()
    if refresh_token is not None:
        client.cookies.set("refresh_token", refresh_token)
    if sub is not None:
        client.cookies.set("refresh_sub", sub)


def _cookie_header(response, name):
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    return None


def _is_cleared(header):
    return header is not None and "Max-Age=0" in header


def _assert_cookies_cleared(response):
    assert _is_cleared(_cookie_header(response, "refresh_token"))
    assert _is_cleared(_cookie_header(response, "refresh_sub"))


class TestLogoutWithRefreshCookie:
    def test_returns_204(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert response.status_code == 204

    def test_calls_cognito_revoke_token(self, client, monkeypatch):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        client.post(ENDPOINT)

        assert len(fake.revoke_calls) == 1
        assert fake.revoke_calls[0]["Token"] == REFRESH_TOKEN

    def test_revoke_token_uses_backend_app_client_credentials(
        self, client, monkeypatch
    ):
        """RevokeToken은 SECRET_HASH가 아니라 ClientSecret 자체를
        파라미터로 받는다(AWS 계약). 값은 settings에서만 읽는다."""
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        client.post(ENDPOINT)

        call = fake.revoke_calls[0]
        assert call["ClientId"] == settings.COGNITO_BACKEND_CLIENT_ID
        assert call["ClientSecret"] == settings.COGNITO_BACKEND_CLIENT_SECRET

    def test_clears_both_cookies_on_success(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        _assert_cookies_cleared(response)

    def test_cleared_cookies_use_the_configured_path(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert "Path=/api/v1/auth" in _cookie_header(response, "refresh_token")

    def test_does_not_require_authorization_header(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        assert client.post(ENDPOINT).status_code == 204


class TestLogoutIsIdempotent:
    def test_no_cookie_returns_204_without_calling_cognito(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        client.cookies.clear()

        response = client.post(ENDPOINT)

        assert response.status_code == 204
        assert fake.revoke_calls == []

    def test_no_cookie_still_clears_cookies(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        client.cookies.clear()

        response = client.post(ENDPOINT)

        _assert_cookies_cleared(response)

    @pytest.mark.parametrize(
        "error_code",
        ["NotAuthorizedException", "TooManyRequestsException", "UnknownException"],
    )
    def test_revoke_failure_still_returns_204_and_clears_cookies(
        self, client, monkeypatch, error_code
    ):
        _patch_cognito(
            monkeypatch, _FakeCognitoClient(error=_client_error(error_code))
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert response.status_code == 204
        _assert_cookies_cleared(response)

    def test_cognito_unreachable_still_returns_204_and_clears_cookies(
        self, client, monkeypatch
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                error=EndpointConnectionError(
                    endpoint_url="https://cognito-idp.example.com"
                )
            ),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert response.status_code == 204
        _assert_cookies_cleared(response)

    def test_unconfigured_backend_app_client_still_returns_204(
        self, client, monkeypatch
    ):
        """COGNITO_BACKEND_CLIENT_ID/SECRET이 아직 주입되지 않은
        환경에서도 로그아웃 자체는 성공해야 한다."""
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert response.status_code == 204
        _assert_cookies_cleared(response)


class TestLogoutSecrecy:
    def test_refresh_token_is_not_logged_on_success(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, refresh_token="super-secret-refresh-abc123")

        with caplog.at_level(logging.DEBUG):
            client.post(ENDPOINT)

        assert "super-secret-refresh-abc123" not in caplog.text

    def test_refresh_token_is_not_logged_on_revoke_failure(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                error=_client_error(
                    "NotAuthorizedException", message="revoked for internal-id-xyz"
                )
            ),
        )
        _set_refresh_cookies(client, refresh_token="super-secret-refresh-abc123")

        with caplog.at_level(logging.DEBUG):
            client.post(ENDPOINT)

        assert "super-secret-refresh-abc123" not in caplog.text

    def test_client_secret_is_not_logged(self, client, monkeypatch, caplog):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(error=_client_error("NotAuthorizedException")),
        )
        _set_refresh_cookies(client)

        with caplog.at_level(logging.DEBUG):
            client.post(ENDPOINT)

        assert settings.COGNITO_BACKEND_CLIENT_SECRET not in caplog.text

    def test_response_body_is_empty(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, refresh_token="super-secret-refresh-abc123")

        response = client.post(ENDPOINT)

        assert response.content == b""
