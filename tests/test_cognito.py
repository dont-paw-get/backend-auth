"""
app/core/cognito.py의 Cognito Access Token 검증 로직 테스트.

CLIAR-105: API 인증에는 Cognito Access Token만 허용하고 ID Token은
거절해야 한다. 이를 위해 verify_cognito_token이 다음을 모두 검증하는지
확인한다:
- JWKS 서명(기존에 이미 검증되던 부분, jwt.decode에 위임)
- exp (만료)
- issuer
- token_use == "access" (ID Token 거절)
- client_id가 현재 Cognito App Client와 일치

실제 Cognito JWKS 서버에 접속하지 않기 위해, get_jwk_client()가 반환하는
PyJWKClient와 jwt.decode 호출 자체를 monkeypatch로 대체한다. 대신 실제
RSA 키 쌍으로 self-signed JWT를 만들어 signature 검증 로직 자체는
실제 jwt 라이브러리를 그대로 통과하게 한다.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import cognito
from app.core.config import settings


@pytest.fixture(scope="module")
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key


@pytest.fixture()
def signing_key_patch(monkeypatch, rsa_key_pair):
    """
    get_jwk_client().get_signing_key_from_jwt(token)이 실제 네트워크
    요청 없이 우리가 만든 RSA public key를 반환하도록 patch한다.
    """

    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    class _FakeJWKClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey(rsa_key_pair.public_key())

    monkeypatch.setattr(cognito, "get_jwk_client", lambda: _FakeJWKClient())
    return rsa_key_pair


def _make_token(
    rsa_key_pair,
    *,
    token_use="access",
    client_id=None,
    aud=None,
    issuer=None,
    exp_delta=3600,
    sub="cognito-sub-0001",
    extra_claims=None,
):
    now = int(time.time())
    payload = {
        "sub": sub,
        "token_use": token_use,
        "iss": issuer if issuer is not None else cognito.COGNITO_ISSUER,
        "iat": now,
        "exp": now + exp_delta,
    }
    if client_id is not None:
        payload["client_id"] = client_id
    if aud is not None:
        payload["aud"] = aud
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, rsa_key_pair, algorithm="RS256")


class TestVerifyCognitoTokenAccepts:
    def test_valid_access_token_is_accepted(self, signing_key_patch):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=settings.COGNITO_CLIENT_ID,
        )

        payload = cognito.verify_cognito_token(token)

        assert payload["sub"] == "cognito-sub-0001"
        assert payload["token_use"] == "access"


class TestVerifyCognitoTokenRejects:
    def test_id_token_is_rejected(self, signing_key_patch):
        """token_use=id인 ID Token은 API 인증 토큰으로 허용하지 않는다."""
        token = _make_token(
            signing_key_patch,
            token_use="id",
            aud=settings.COGNITO_CLIENT_ID,
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_wrong_client_id_is_rejected(self, signing_key_patch):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id="some-other-client-id",
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_missing_client_id_claim_is_rejected(self, signing_key_patch):
        token = _make_token(signing_key_patch, token_use="access", client_id=None)

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_expired_token_is_rejected(self, signing_key_patch):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=settings.COGNITO_CLIENT_ID,
            exp_delta=-3600,
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_wrong_issuer_is_rejected(self, signing_key_patch):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=settings.COGNITO_CLIENT_ID,
            issuer="https://cognito-idp.ap-northeast-2.amazonaws.com/attacker-pool",
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_missing_sub_is_rejected(self, signing_key_patch):
        """
        sub claim이 없는 토큰은 명세상 정상 Access Token일 수 없지만,
        방어적으로 sub 자체 존재도 확인한다(get_current_user_id 쪽의
        기존 검사와 중복되더라도, verify_cognito_token 계약을 명확히
        하기 위함).
        """
        now = int(time.time())
        payload = {
            "token_use": "access",
            "client_id": settings.COGNITO_CLIENT_ID,
            "iss": cognito.COGNITO_ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(payload, signing_key_patch, algorithm="RS256")

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_invalid_signature_is_rejected(self):
        """서명이 아예 다른 키로 만들어진 토큰(신뢰할 수 없는 발급자)은
        거절되어야 한다. 이 테스트는 get_jwk_client를 patch하지 않아
        실제 JWKS 조회를 시도하다 실패하는 경로도 결국 ValueError로
        수렴하는지 확인한다."""
        fake_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        payload = {
            "sub": "attacker",
            "token_use": "access",
            "client_id": settings.COGNITO_CLIENT_ID,
            "iss": cognito.COGNITO_ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(payload, fake_key, algorithm="RS256")

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)


class TestGetCognitoUserEmail:
    """
    Cognito GetUser 호출로 email을 얻는 get_cognito_user_email 테스트.

    boto3 client 자체를 monkeypatch하여 실제 AWS/Cognito에 접속하지
    않는다.
    """

    def _patch_client(self, monkeypatch, fake_client):
        monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: fake_client)

    def test_returns_email_from_user_attributes(self, monkeypatch):
        class _FakeClient:
            def get_user(self, AccessToken):
                return {
                    "UserAttributes": [
                        {"Name": "sub", "Value": "cognito-sub-0001"},
                        {"Name": "email", "Value": "user@example.com"},
                        {"Name": "email_verified", "Value": "true"},
                    ]
                }

        self._patch_client(monkeypatch, _FakeClient())

        email = cognito.get_cognito_user_email("some-access-token")

        assert email == "user@example.com"

    def test_missing_email_attribute_raises_value_error(self, monkeypatch):
        class _FakeClient:
            def get_user(self, AccessToken):
                return {"UserAttributes": [{"Name": "sub", "Value": "cognito-sub-0001"}]}

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(ValueError):
            cognito.get_cognito_user_email("some-access-token")

    def test_cognito_rejecting_token_raises_value_error(self, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def get_user(self, AccessToken):
                raise ClientError(
                    {"Error": {"Code": "NotAuthorizedException", "Message": "Invalid token"}},
                    "GetUser",
                )

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(ValueError):
            cognito.get_cognito_user_email("expired-or-revoked-token")

    def test_cognito_service_error_raises_runtime_error(self, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def get_user(self, AccessToken):
                raise ClientError(
                    {"Error": {"Code": "InternalErrorException", "Message": "boom"}},
                    "GetUser",
                )

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(RuntimeError):
            cognito.get_cognito_user_email("some-access-token")

    def test_network_failure_raises_runtime_error(self, monkeypatch):
        from botocore.exceptions import EndpointConnectionError

        class _FakeClient:
            def get_user(self, AccessToken):
                raise EndpointConnectionError(endpoint_url="https://cognito-idp.example.com")

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(RuntimeError):
            cognito.get_cognito_user_email("some-access-token")


class TestDeleteCognitoUser:
    """
    CLIAR-113: DeleteUser(access token 기반 self-service 삭제) 테스트.

    boto3 client 자체를 monkeypatch하여 실제 AWS/Cognito에 접속하지
    않는다.
    """

    def _patch_client(self, monkeypatch, fake_client):
        monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: fake_client)

    def test_calls_delete_user_with_access_token(self, monkeypatch):
        received = {}

        class _FakeClient:
            def delete_user(self, AccessToken):
                received["AccessToken"] = AccessToken

        self._patch_client(monkeypatch, _FakeClient())

        cognito.delete_cognito_user("some-access-token", sub="cognito-sub-0001")

        assert received["AccessToken"] == "some-access-token"

    def test_success_returns_none(self, monkeypatch):
        class _FakeClient:
            def delete_user(self, AccessToken):
                return None

        self._patch_client(monkeypatch, _FakeClient())

        result = cognito.delete_cognito_user("some-access-token", sub="cognito-sub-0001")

        assert result is None

    def test_user_not_found_raises_already_deleted_error(self, monkeypatch):
        """
        DeleteUser는 username이 아니라 access token으로만 대상을
        특정하므로, UserNotFoundException은 "이 토큰이 가리키던
        사용자가 이미 User Pool에 없다"로만 안전하게 해석할 수 있다.
        """
        from botocore.exceptions import ClientError

        class _FakeClient:
            def delete_user(self, AccessToken):
                raise ClientError(
                    {"Error": {"Code": "UserNotFoundException", "Message": "not found"}},
                    "DeleteUser",
                )

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(cognito.CognitoUserAlreadyDeletedError):
            cognito.delete_cognito_user("some-access-token", sub="cognito-sub-0001")

    def test_not_authorized_raises_value_error_not_already_deleted(self, monkeypatch):
        """
        NotAuthorizedException만으로는 토큰이 무효한 것과 사용자가
        이미 삭제된 것을 구분할 수 없으므로, 이를 성공(이미 삭제됨)으로
        간주하지 않고 ValueError(401 매핑용)로 처리해야 한다.
        """
        from botocore.exceptions import ClientError

        class _FakeClient:
            def delete_user(self, AccessToken):
                raise ClientError(
                    {"Error": {"Code": "NotAuthorizedException", "Message": "invalid"}},
                    "DeleteUser",
                )

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(ValueError):
            cognito.delete_cognito_user("some-access-token", sub="cognito-sub-0001")

        # NotAuthorizedException은 CognitoUserAlreadyDeletedError가 아니다.
        try:
            cognito.delete_cognito_user("some-access-token", sub="cognito-sub-0001")
        except cognito.CognitoUserAlreadyDeletedError:
            pytest.fail("NotAuthorizedException must not be treated as already-deleted")
        except ValueError:
            pass

    def test_service_error_raises_runtime_error(self, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def delete_user(self, AccessToken):
                raise ClientError(
                    {"Error": {"Code": "InternalErrorException", "Message": "boom"}},
                    "DeleteUser",
                )

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(RuntimeError):
            cognito.delete_cognito_user("some-access-token", sub="cognito-sub-0001")

    def test_network_failure_raises_runtime_error(self, monkeypatch):
        from botocore.exceptions import EndpointConnectionError

        class _FakeClient:
            def delete_user(self, AccessToken):
                raise EndpointConnectionError(endpoint_url="https://cognito-idp.example.com")

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(RuntimeError):
            cognito.delete_cognito_user("some-access-token", sub="cognito-sub-0001")


class TestRefreshCognitoAccessToken:
    """
    CLIAR-125: Cognito Refresh Token으로 새 Access Token을 발급받는
    refresh_cognito_access_token() 테스트.

    boto3 client 자체를 monkeypatch하여 실제 AWS/Cognito에 접속하지
    않는다.
    """

    def _patch_client(self, monkeypatch, fake_client):
        monkeypatch.setattr(cognito, "get_cognito_idp_client", lambda: fake_client)

    def test_calls_initiate_auth_with_refresh_token_auth_flow(self, monkeypatch):
        received = {}

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                received["AuthFlow"] = AuthFlow
                received["AuthParameters"] = AuthParameters
                received["ClientId"] = ClientId
                return {
                    "AuthenticationResult": {
                        "AccessToken": "new-access-token",
                        "ExpiresIn": 86400,
                    }
                }

        self._patch_client(monkeypatch, _FakeClient())

        cognito.refresh_cognito_access_token("some-refresh-token")

        assert received["AuthFlow"] == "REFRESH_TOKEN_AUTH"

    def test_passes_refresh_token_in_auth_parameters(self, monkeypatch):
        received = {}

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                received["AuthParameters"] = AuthParameters
                return {
                    "AuthenticationResult": {
                        "AccessToken": "new-access-token",
                        "ExpiresIn": 86400,
                    }
                }

        self._patch_client(monkeypatch, _FakeClient())

        cognito.refresh_cognito_access_token("some-refresh-token")

        assert received["AuthParameters"] == {"REFRESH_TOKEN": "some-refresh-token"}

    def test_uses_client_id_from_settings_not_hardcoded(self, monkeypatch):
        received = {}

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                received["ClientId"] = ClientId
                return {
                    "AuthenticationResult": {
                        "AccessToken": "new-access-token",
                        "ExpiresIn": 86400,
                    }
                }

        self._patch_client(monkeypatch, _FakeClient())

        cognito.refresh_cognito_access_token("some-refresh-token")

        assert received["ClientId"] == settings.COGNITO_CLIENT_ID

    def test_success_returns_authentication_result(self, monkeypatch):
        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                return {
                    "AuthenticationResult": {
                        "AccessToken": "new-access-token",
                        "ExpiresIn": 86400,
                        "TokenType": "Bearer",
                    }
                }

        self._patch_client(monkeypatch, _FakeClient())

        result = cognito.refresh_cognito_access_token("some-refresh-token")

        assert result["AccessToken"] == "new-access-token"
        assert result["ExpiresIn"] == 86400

    def test_response_does_not_include_new_refresh_token_when_rotation_disabled(
        self, monkeypatch
    ):
        """Refresh Token Rotation이 비활성화된 App Client는 Cognito가
        새 refresh token을 내려주지 않는다. 이 함수는 그런 값을
        임의로 만들어내지 않고 Cognito 응답을 그대로 전달해야 한다."""

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                return {
                    "AuthenticationResult": {
                        "AccessToken": "new-access-token",
                        "ExpiresIn": 86400,
                    }
                }

        self._patch_client(monkeypatch, _FakeClient())

        result = cognito.refresh_cognito_access_token("some-refresh-token")

        assert "RefreshToken" not in result

    def test_not_authorized_raises_value_error(self, monkeypatch):
        """만료되었거나 잘못된 Refresh Token은 Cognito가
        NotAuthorizedException으로 거절한다."""
        from botocore.exceptions import ClientError

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                raise ClientError(
                    {"Error": {"Code": "NotAuthorizedException", "Message": "invalid"}},
                    "InitiateAuth",
                )

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(ValueError):
            cognito.refresh_cognito_access_token("bad-refresh-token")

    def test_service_error_raises_runtime_error(self, monkeypatch):
        from botocore.exceptions import ClientError

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                raise ClientError(
                    {"Error": {"Code": "InternalErrorException", "Message": "boom"}},
                    "InitiateAuth",
                )

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(RuntimeError):
            cognito.refresh_cognito_access_token("some-refresh-token")

    def test_network_failure_raises_runtime_error(self, monkeypatch):
        from botocore.exceptions import EndpointConnectionError

        class _FakeClient:
            def initiate_auth(self, AuthFlow, AuthParameters, ClientId):
                raise EndpointConnectionError(endpoint_url="https://cognito-idp.example.com")

        self._patch_client(monkeypatch, _FakeClient())

        with pytest.raises(RuntimeError):
            cognito.refresh_cognito_access_token("some-refresh-token")
