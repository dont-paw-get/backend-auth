"""
POST /api/v1/auth/refresh — 쿠키 기반 최종 계약 테스트
(CLIAR-153, Phase 4).

최종 계약: request body 없음. refresh_token / refresh_sub HttpOnly
쿠키만 사용하며, 신규 backend App Client(secret 있음)로
REFRESH_TOKEN_AUTH를 호출한다. SECRET_HASH의 username은 반드시
refresh_sub(=Cognito sub)여야 한다(refresh token은 opaque 문자열이라
BE가 sub를 추출할 수 없다).

CLIAR-125의 legacy body 기반 refresh 계약이 그대로 유지되는지는
tests/test_auth_refresh.py가 계속 검증한다. 이 파일은 신규 쿠키
경로와, 두 경로가 공존할 때의 우선순위만 다룬다.

실제 AWS/Cognito에는 접속하지 않는다.
"""

import logging

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app.core import cognito, cognito_auth
from app.core.cognito_auth import secret_hash
from app.core.config import settings
from app.main import app

ENDPOINT = "/api/v1/auth/refresh"

REFRESH_SUB = "11111111-2222-3333-4444-555555555555"
REFRESH_TOKEN = "stored-refresh-token"


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
    """InitiateAuth(REFRESH_TOKEN_AUTH)를 흉내내는 fake Cognito client."""

    def __init__(self, *, error=None, auth_result=None):
        self.error = error
        self.auth_result = (
            auth_result
            if auth_result is not None
            else {
                "AccessToken": "refreshed-access-token",
                "IdToken": "refreshed-id-token",
                "ExpiresIn": 86400,
                "TokenType": "Bearer",
            }
        )
        self.initiate_auth_calls = []

    def initiate_auth(self, **kwargs):
        self.initiate_auth_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"AuthenticationResult": dict(self.auth_result)}


def _patch_cognito(monkeypatch, fake_client):
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: fake_client)
    return fake_client


def _client_error(code, message="cognito message"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "InitiateAuth")


def _set_refresh_cookies(client, *, refresh_token=REFRESH_TOKEN, sub=REFRESH_SUB):
    """
    TestClient의 쿠키 jar에 직접 값을 심는다.

    서버가 내려준 Set-Cookie를 그대로 재사용하지 않는 이유: 실제
    쿠키는 Secure 속성이 붙어 있어 TestClient의 http://testserver
    요청에는 다시 전송되지 않기 때문이다.
    """
    client.cookies.clear()
    if refresh_token is not None:
        client.cookies.set("refresh_token", refresh_token)
    if sub is not None:
        client.cookies.set("refresh_sub", sub)


def _set_cookie_headers(response):
    return response.headers.get_list("set-cookie")


def _cookie_header(response, name):
    for header in _set_cookie_headers(response):
        if header.startswith(f"{name}="):
            return header
    return None


def _is_cleared(header):
    return header is not None and 'Max-Age=0' in header


class TestCookieRefreshSuccess:
    def test_valid_cookies_return_200(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert response.status_code == 200

    def test_response_contains_new_tokens(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        body = client.post(ENDPOINT).json()

        assert body["access_token"] == "refreshed-access-token"
        assert body["id_token"] == "refreshed-id-token"
        assert body["expires_in"] == 86400
        assert body["token_type"] == "Bearer"

    def test_response_body_does_not_contain_refresh_token(
        self, client, monkeypatch
    ):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert "refresh_token" not in response.json()

    def test_uses_refresh_token_auth_flow(self, client, monkeypatch):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        client.post(ENDPOINT)

        assert fake.initiate_auth_calls[0]["AuthFlow"] == "REFRESH_TOKEN_AUTH"

    def test_uses_backend_app_client_id(self, client, monkeypatch):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        client.post(ENDPOINT)

        assert (
            fake.initiate_auth_calls[0]["ClientId"]
            == settings.COGNITO_BACKEND_CLIENT_ID
        )

    def test_refresh_token_cookie_value_is_forwarded_to_cognito(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, refresh_token="exact-cookie-value-123")

        client.post(ENDPOINT)

        assert (
            fake.initiate_auth_calls[0]["AuthParameters"]["REFRESH_TOKEN"]
            == "exact-cookie-value-123"
        )

    def test_secret_hash_username_is_the_refresh_sub_cookie(
        self, client, monkeypatch
    ):
        """
        핵심 계약: SECRET_HASH는 email이 아니라 refresh_sub(sub)로
        계산되어야 한다.
        """
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, sub=REFRESH_SUB)

        client.post(ENDPOINT)

        parameters = fake.initiate_auth_calls[0]["AuthParameters"]
        assert parameters["SECRET_HASH"] == secret_hash(REFRESH_SUB)
        assert parameters["SECRET_HASH"] != secret_hash("user@example.com")

    def test_username_is_not_sent_for_refresh_token_auth(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        client.post(ENDPOINT)

        assert "USERNAME" not in fake.initiate_auth_calls[0]["AuthParameters"]


class TestCookieRefreshRotation:
    def test_new_refresh_token_updates_the_cookie(self, client, monkeypatch):
        """Refresh Token Rotation이 활성화되면 Cognito가 새 refresh
        token을 반환한다. 그 경우 쿠키를 갱신해야 한다."""
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                auth_result={
                    "AccessToken": "refreshed-access-token",
                    "IdToken": "refreshed-id-token",
                    "RefreshToken": "rotated-refresh-token",
                    "ExpiresIn": 86400,
                    "TokenType": "Bearer",
                }
            ),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        header = _cookie_header(response, "refresh_token")
        assert header is not None
        assert "rotated-refresh-token" in header
        assert "HttpOnly" in header

    def test_refresh_sub_cookie_is_kept_on_rotation(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                auth_result={
                    "AccessToken": "refreshed-access-token",
                    "RefreshToken": "rotated-refresh-token",
                    "ExpiresIn": 86400,
                    "TokenType": "Bearer",
                }
            ),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        header = _cookie_header(response, "refresh_sub")
        assert header is not None
        assert REFRESH_SUB in header
        assert not _is_cleared(header)

    def test_rotated_refresh_token_is_not_returned_in_the_body(
        self, client, monkeypatch
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                auth_result={
                    "AccessToken": "refreshed-access-token",
                    "RefreshToken": "rotated-refresh-token",
                    "ExpiresIn": 86400,
                    "TokenType": "Bearer",
                }
            ),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert "rotated-refresh-token" not in response.text

    def test_no_cookie_is_rewritten_when_rotation_is_disabled(
        self, client, monkeypatch
    ):
        """현재 dev App Client는 rotation 비활성이므로 Cognito가 새
        refresh token을 주지 않는다. 그 경우 기존 쿠키를 건드리지
        않는다."""
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert _set_cookie_headers(response) == []


class TestCookieRefreshMissingCookies:
    def test_no_cookies_and_no_body_returns_401(self, client, monkeypatch):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        client.cookies.clear()

        response = client.post(ENDPOINT)

        assert response.status_code == 401
        assert fake.initiate_auth_calls == []

    def test_missing_refresh_sub_cookie_returns_401(self, client, monkeypatch):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, sub=None)

        response = client.post(ENDPOINT)

        assert response.status_code == 401
        assert fake.initiate_auth_calls == []

    def test_missing_refresh_sub_cookie_clears_the_remaining_cookie(
        self, client, monkeypatch
    ):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, sub=None)

        response = client.post(ENDPOINT)

        assert _is_cleared(_cookie_header(response, "refresh_token"))
        assert _is_cleared(_cookie_header(response, "refresh_sub"))

    def test_missing_refresh_token_cookie_with_only_sub_returns_401(
        self, client, monkeypatch
    ):
        """refresh_sub만 남아 있고 refresh_token이 없는 경우도 쿠키
        모드로 간주해 401 + clear로 실패시켜야 한다(legacy로 빠지지
        않는다)."""
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, refresh_token=None)

        response = client.post(ENDPOINT)

        assert response.status_code == 401
        assert fake.initiate_auth_calls == []
        assert _is_cleared(_cookie_header(response, "refresh_token"))
        assert _is_cleared(_cookie_header(response, "refresh_sub"))

    def test_refresh_sub_only_with_legacy_body_does_not_call_legacy_cognito(
        self, client, monkeypatch
    ):
        """refresh_sub만 있고 refresh_token 쿠키가 없는 상태에서
        legacy body가 함께 오더라도, 쿠키가 하나라도 존재하는 순간
        쿠키 모드로 처리되어 legacy Cognito 호출로 넘어가면 안
        된다."""
        backend_client = _patch_cognito(monkeypatch, _FakeCognitoClient())

        legacy_calls = []

        class _LegacyClient:
            def initiate_auth(self, **kwargs):  # pragma: no cover - 호출되면 실패
                legacy_calls.append(kwargs)
                return {
                    "AuthenticationResult": {"AccessToken": "legacy", "ExpiresIn": 1}
                }

        monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: _LegacyClient())
        _set_refresh_cookies(client, refresh_token=None)

        response = client.post(ENDPOINT, json={"refresh_token": "legacy-body-token"})

        assert legacy_calls == []
        assert backend_client.initiate_auth_calls == []
        assert response.status_code == 401
        assert _is_cleared(_cookie_header(response, "refresh_token"))
        assert _is_cleared(_cookie_header(response, "refresh_sub"))

    def test_refresh_sub_only_without_body_returns_401_and_clears_cookies(
        self, client, monkeypatch
    ):
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, refresh_token=None)

        response = client.post(ENDPOINT)

        assert response.status_code == 401
        assert fake.initiate_auth_calls == []
        assert _is_cleared(_cookie_header(response, "refresh_token"))
        assert _is_cleared(_cookie_header(response, "refresh_sub"))


class TestCookieRefreshCognitoErrors:
    def test_not_authorized_returns_401(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(error=_client_error("NotAuthorizedException")),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert response.status_code == 401

    def test_not_authorized_clears_both_cookies(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(error=_client_error("NotAuthorizedException")),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert _is_cleared(_cookie_header(response, "refresh_token"))
        assert _is_cleared(_cookie_header(response, "refresh_sub"))

    def test_cleared_cookies_use_the_same_path_and_attributes(
        self, client, monkeypatch
    ):
        """delete_cookie가 set_cookie와 동일한 path/secure/samesite로
        나가야 브라우저가 실제로 쿠키를 지운다."""
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(error=_client_error("NotAuthorizedException")),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        header = _cookie_header(response, "refresh_token")
        assert "Path=/api/v1/auth" in header
        assert "SameSite=lax" in header

    @pytest.mark.parametrize(
        "error_code",
        [
            "TooManyRequestsException",
            "LimitExceededException",
            "TooManyFailedAttemptsException",
        ],
    )
    def test_rate_limit_errors_return_429(self, client, monkeypatch, error_code):
        _patch_cognito(monkeypatch, _FakeCognitoClient(error=_client_error(error_code)))
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert response.status_code == 429

    def test_rate_limited_refresh_does_not_clear_cookies(
        self, client, monkeypatch
    ):
        """429는 일시적인 실패이므로 세션을 버리지 않는다."""
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(error=_client_error("TooManyRequestsException")),
        )
        _set_refresh_cookies(client)

        response = client.post(ENDPOINT)

        assert _set_cookie_headers(response) == []

    def test_connection_error_returns_502(self, client, monkeypatch):
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

        assert response.status_code == 502

    def test_error_response_does_not_leak_cognito_message_or_token(
        self, client, monkeypatch
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                error=_client_error(
                    "NotAuthorizedException",
                    message="Refresh Token has been revoked for user internal-id-xyz",
                )
            ),
        )
        _set_refresh_cookies(client, refresh_token="super-secret-refresh-abc123")

        response = client.post(ENDPOINT)

        assert "internal-id-xyz" not in response.text
        assert "super-secret-refresh-abc123" not in response.text

    def test_tokens_are_not_logged(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeCognitoClient())
        _set_refresh_cookies(client, refresh_token="super-secret-refresh-abc123")

        with caplog.at_level(logging.DEBUG):
            client.post(ENDPOINT)

        assert "super-secret-refresh-abc123" not in caplog.text
        assert "refreshed-access-token" not in caplog.text


class TestCookieRefreshTakesPrecedenceOverLegacyBody:
    """
    과도기 동안 두 계약이 공존한다. 쿠키가 있으면 항상 신규 방식이
    우선해야 한다(legacy body가 함께 와도 무시).
    """

    def test_cookie_path_wins_when_both_cookie_and_body_are_present(
        self, client, monkeypatch
    ):
        backend_client = _patch_cognito(monkeypatch, _FakeCognitoClient())

        legacy_calls = []

        class _LegacyClient:
            def initiate_auth(self, **kwargs):  # pragma: no cover - 호출되면 실패
                legacy_calls.append(kwargs)
                return {"AuthenticationResult": {"AccessToken": "legacy", "ExpiresIn": 1}}

        monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: _LegacyClient())
        _set_refresh_cookies(client)

        body = client.post(
            ENDPOINT, json={"refresh_token": "legacy-body-token"}
        ).json()

        assert legacy_calls == []
        assert body["access_token"] == "refreshed-access-token"
        assert (
            backend_client.initiate_auth_calls[0]["AuthParameters"]["REFRESH_TOKEN"]
            == REFRESH_TOKEN
        )

    def test_legacy_body_is_used_only_when_no_cookie_is_present(
        self, client, monkeypatch
    ):
        backend_client = _patch_cognito(monkeypatch, _FakeCognitoClient())

        class _LegacyClient:
            def __init__(self):
                self.calls = []

            def initiate_auth(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "AuthenticationResult": {
                        "AccessToken": "legacy-access-token",
                        "ExpiresIn": 3600,
                    }
                }

        legacy_client = _LegacyClient()
        monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: legacy_client)
        client.cookies.clear()

        response = client.post(
            ENDPOINT, json={"refresh_token": "legacy-body-token"}
        )
        body = response.json()

        assert response.status_code == 200
        assert body["access_token"] == "legacy-access-token"
        assert backend_client.initiate_auth_calls == []
        # legacy 경로는 기존 FE App Client를 쓰고 SECRET_HASH를 보내지
        # 않는다(신규 backend secret을 억지로 적용하지 않는다).
        assert legacy_client.calls[0]["ClientId"] == settings.COGNITO_CLIENT_ID
        assert legacy_client.calls[0]["AuthParameters"] == {
            "REFRESH_TOKEN": "legacy-body-token"
        }

    def test_no_cookies_at_all_still_uses_legacy_body_successfully(
        self, client, monkeypatch
    ):
        """A. 두 쿠키가 모두 없는 경우: 기존 CLIAR-125 legacy refresh가
        그대로 성공해야 한다(회귀 없음)."""
        backend_client = _patch_cognito(monkeypatch, _FakeCognitoClient())

        class _LegacyClient:
            def __init__(self):
                self.calls = []

            def initiate_auth(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "AuthenticationResult": {
                        "AccessToken": "legacy-access-token",
                        "ExpiresIn": 3600,
                        "TokenType": "Bearer",
                    }
                }

        legacy_client = _LegacyClient()
        monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: legacy_client)
        client.cookies.clear()

        response = client.post(
            ENDPOINT, json={"refresh_token": "legacy-body-token"}
        )

        assert response.status_code == 200
        assert response.json()["access_token"] == "legacy-access-token"
        assert backend_client.initiate_auth_calls == []
        assert len(legacy_client.calls) == 1
