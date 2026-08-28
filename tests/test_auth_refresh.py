"""
POST /api/v1/auth/refresh 테스트 (CLIAR-125).

Access Token이 만료됐을 때 Cognito Refresh Token으로 새 Access
Token을 재발급받는 API. 실제 AWS Cognito에 접속하지 않기 위해
app.core.cognito.get_cognito_idp_client를 monkeypatch한다.

이 endpoint는 Bearer Access Token 인증을 요구하지 않으므로(만료된
Access Token으로는 호출할 수 없어야 하는 API이기 때문), 다른 인증이
필요한 라우터 테스트와 달리 별도의 인증 override가 필요 없다.
"""

import pytest
from fastapi.testclient import TestClient

from app.core import cognito
from app.main import app

ENDPOINT = "/api/v1/auth/refresh"


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _patch_initiate_auth(monkeypatch, fake_client):
    monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: fake_client)


class _FakeSuccessClient:
    """정상적으로 새 Access Token을 발급하는 Cognito client."""

    def __init__(self):
        self.received_kwargs = None

    def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
        self.received_kwargs = {
            "AuthFlow": AuthFlow,
            "AuthParameters": AuthParameters,
            "ClientId": ClientId,
        }
        return {
            "AuthenticationResult": {
                "AccessToken": "brand-new-access-token",
                "ExpiresIn": 86400,
                "TokenType": "Bearer",
            }
        }


class TestRefreshTokenSuccess:
    def test_valid_refresh_token_returns_200(self, client, monkeypatch):
        _patch_initiate_auth(monkeypatch, _FakeSuccessClient())

        response = client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert response.status_code == 200

    def test_response_contains_access_token(self, client, monkeypatch):
        _patch_initiate_auth(monkeypatch, _FakeSuccessClient())

        response = client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        body = response.json()
        assert body["access_token"] == "brand-new-access-token"

    def test_response_token_type_is_bearer(self, client, monkeypatch):
        _patch_initiate_auth(monkeypatch, _FakeSuccessClient())

        response = client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert response.json()["token_type"] == "Bearer"

    def test_response_contains_expires_in(self, client, monkeypatch):
        _patch_initiate_auth(monkeypatch, _FakeSuccessClient())

        response = client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert response.json()["expires_in"] == 86400

    def test_cognito_called_with_refresh_token_auth_flow(self, client, monkeypatch):
        fake_client = _FakeSuccessClient()
        _patch_initiate_auth(monkeypatch, fake_client)

        client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert fake_client.received_kwargs["AuthFlow"] == "REFRESH_TOKEN_AUTH"

    def test_request_refresh_token_is_forwarded_to_cognito_auth_parameters(
        self, client, monkeypatch
    ):
        fake_client = _FakeSuccessClient()
        _patch_initiate_auth(monkeypatch, fake_client)

        client.post(ENDPOINT, json={"refresh_token": "exact-token-value-123"})

        assert fake_client.received_kwargs["AuthParameters"] == {
            "REFRESH_TOKEN": "exact-token-value-123"
        }

    def test_client_id_is_settings_cognito_client_id(self, client, monkeypatch):
        from app.core.config import settings

        fake_client = _FakeSuccessClient()
        _patch_initiate_auth(monkeypatch, fake_client)

        client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert fake_client.received_kwargs["ClientId"] == settings.COGNITO_CLIENT_ID

    def test_response_does_not_contain_a_new_refresh_token(self, client, monkeypatch):
        """Refresh Token Rotation이 비활성화되어 있으므로, 응답에
        새로운 refresh_token을 만들어 포함하지 않아야 한다."""
        _patch_initiate_auth(monkeypatch, _FakeSuccessClient())

        response = client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert "refresh_token" not in response.json()

    def test_does_not_require_authorization_header(self, client, monkeypatch):
        """만료된 Access Token으로는 호출할 수 없어야 하는 API이므로
        Authorization 헤더 없이 호출 가능해야 한다."""
        _patch_initiate_auth(monkeypatch, _FakeSuccessClient())

        response = client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert response.status_code == 200


class TestRefreshTokenValidation:
    def test_empty_refresh_token_returns_422(self, client):
        response = client.post(ENDPOINT, json={"refresh_token": ""})

        assert response.status_code == 422

    def test_blank_refresh_token_returns_422(self, client):
        response = client.post(ENDPOINT, json={"refresh_token": "   "})

        assert response.status_code == 422

    def test_missing_refresh_token_key_returns_422(self, client):
        response = client.post(ENDPOINT, json={})

        assert response.status_code == 422

    def test_unexpected_field_returns_422(self, client):
        response = client.post(
            ENDPOINT,
            json={"refresh_token": "valid-refresh-token", "user_id": "attacker-supplied"},
        )

        assert response.status_code == 422


class TestRefreshTokenCognitoErrors:
    def test_invalid_or_expired_refresh_token_returns_401(self, client, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                raise ClientError(
                    {"Error": {"Code": "NotAuthorizedException", "Message": "Invalid Refresh Token"}},
                    "InitiateAuth",
                )

        _patch_initiate_auth(monkeypatch, _FakeClient())

        response = client.post(ENDPOINT, json={"refresh_token": "expired-or-revoked-token"})

        assert response.status_code == 401

    def test_cognito_service_outage_returns_502(self, client, monkeypatch):
        from botocore.exceptions import EndpointConnectionError

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                raise EndpointConnectionError(endpoint_url="https://cognito-idp.example.com")

        _patch_initiate_auth(monkeypatch, _FakeClient())

        response = client.post(ENDPOINT, json={"refresh_token": "valid-refresh-token"})

        assert response.status_code == 502

    def test_error_response_does_not_leak_cognito_message(self, client, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "NotAuthorizedException",
                            "Message": "Refresh Token has been revoked for user internal-id-xyz",
                        }
                    },
                    "InitiateAuth",
                )

        _patch_initiate_auth(monkeypatch, _FakeClient())

        response = client.post(ENDPOINT, json={"refresh_token": "expired-or-revoked-token"})

        body_text = response.text
        assert "internal-id-xyz" not in body_text
        assert "revoked for user" not in body_text

    def test_error_response_does_not_leak_input_refresh_token(self, client, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                raise ClientError(
                    {"Error": {"Code": "NotAuthorizedException", "Message": "invalid"}},
                    "InitiateAuth",
                )

        _patch_initiate_auth(monkeypatch, _FakeClient())

        secret_token_value = "super-secret-refresh-token-value-abc123"
        response = client.post(ENDPOINT, json={"refresh_token": secret_token_value})

        assert secret_token_value not in response.text
