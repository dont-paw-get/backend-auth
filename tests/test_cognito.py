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

from app.core import cognito, security
from app.core.config import settings

# CLIAR-162 Phase 7: COGNITO_CLIENT_ID(기존 FE App Client) 설정 자체가
# app/core/config.py에서 제거되었으므로, "기존 FE client_id"를
# 표현해야 하는 테스트는 임의의 고정 문자열을 대신 사용한다.
_LEGACY_FE_CLIENT_ID = "legacy-fe-app-client-id"


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


@pytest.fixture(autouse=True)
def backend_client_id_configured(monkeypatch):
    """
    이 파일의 모든 테스트는 기본적으로 COGNITO_BACKEND_CLIENT_ID가
    정상적으로 배포되어 있는 상태를 전제로 한다(CLIAR-162 Phase 7
    최종: backend App Client만 허용). 설정 누락(None/빈 문자열) 자체를
    검증하는 테스트는 이 fixture가 이미 설정한 값을 각자
    monkeypatch로 다시 덮어쓴다.
    """
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-app-client-id")


class TestVerifyCognitoTokenAccepts:
    def test_valid_backend_access_token_is_accepted(self, signing_key_patch):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=settings.COGNITO_BACKEND_CLIENT_ID,
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
            aud=settings.COGNITO_BACKEND_CLIENT_ID,
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_legacy_fe_client_id_is_rejected(self, signing_key_patch):
        """
        CLIAR-162 Phase 7 최종 전환: 기존 FE App Client 토큰은 더 이상
        허용하지 않는다(설정 자체도 함께 제거됨, app/core/config.py
        참고). 프론트 인증 연동이 처음부터 없었음이 확인되어 과도기
        dual-accept(Phase 7A)를 종료했다.
        """
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=_LEGACY_FE_CLIENT_ID,
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_third_party_client_id_is_rejected(self, signing_key_patch):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id="some-attacker-controlled-client-id",
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_backend_client_id_none_rejects_every_token(
        self, signing_key_patch, monkeypatch
    ):
        """
        COGNITO_BACKEND_CLIENT_ID가 배포 환경에 주입되지 않아 None이면
        (autouse fixture를 이 테스트가 덮어씀), 어떤 client_id를 가진
        토큰도 통과시키면 안 된다 — 설정 누락이 "아무 client_id나
        허용"하는 인증 우회로 이어지지 않아야 한다.
        """
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id="would-be-backend-client-id",
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_backend_client_id_blank_string_rejects_every_token(
        self, signing_key_patch, monkeypatch
    ):
        """
        COGNITO_BACKEND_CLIENT_ID가 빈 문자열/공백뿐이면 그 값 자체를
        허용 client_id로 취급하지 않는다. client_id claim이 빈
        문자열인 토큰(비정상적인 형태)도 통과시키지 않는다.
        """
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "   ")
        now = int(time.time())
        payload = {
            "sub": "cognito-sub-0001",
            "token_use": "access",
            "client_id": "",
            "iss": cognito.COGNITO_ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(payload, signing_key_patch, algorithm="RS256")

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
            client_id=settings.COGNITO_BACKEND_CLIENT_ID,
            exp_delta=-3600,
        )

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)

    def test_wrong_issuer_is_rejected(self, signing_key_patch):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=settings.COGNITO_BACKEND_CLIENT_ID,
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
            "client_id": settings.COGNITO_BACKEND_CLIENT_ID,
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
            "client_id": settings.COGNITO_BACKEND_CLIENT_ID,
            "iss": cognito.COGNITO_ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(payload, fake_key, algorithm="RS256")

        with pytest.raises(ValueError):
            cognito.verify_cognito_token(token)


class TestRequiredClientId:
    """
    _required_client_id()(CLIAR-162 Phase 7 최종)의 순수 함수 단위
    테스트. JWT/JWKS 없이 settings만으로 확인한다.
    """

    def test_returns_backend_client_id_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client")

        assert cognito._required_client_id() == "backend-client"

    def test_returns_none_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)

        assert cognito._required_client_id() is None

    def test_returns_none_when_blank_string(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "   ")

        assert cognito._required_client_id() is None

    def test_reflects_current_settings_on_each_call(self, monkeypatch):
        """
        모듈 임포트 시점에 고정되지 않고, 호출할 때마다 현재
        settings 값을 다시 읽어야 한다(재기동 없이 monkeypatch로
        값이 바뀌면 즉시 반영되는지 확인).
        """
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)
        assert cognito._required_client_id() is None

        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client")
        assert cognito._required_client_id() == "backend-client"


class TestSecurityDependencyAcceptsBackendClientOnly:
    """
    CLIAR-162 Phase 7 최종: /users/me 등이 실제로 사용하는 FastAPI
    dependency 체인(app/core/security.py의
    _extract_and_verify_bearer_token -> get_current_user_id) 수준에서도
    backend client_id 토큰만 통과하고, 기존 FE/제3자 client_id는
    401이 되는지 확인한다.

    dependency 함수를 FastAPI 없이 직접 호출한다 — security.py의
    Header(default=None, ...) 파라미터는 명시적으로 문자열 인자를
    넘기면 그대로 그 값이 쓰인다(FastAPI Depends() 경유 시에만 실제
    요청 헤더로 채워지는 sentinel일 뿐이므로, 직접 호출에서는 일반
    함수 인자와 동일하게 동작한다).
    """

    def test_backend_client_token_passes_get_current_user_id(
        self, signing_key_patch
    ):
        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=settings.COGNITO_BACKEND_CLIENT_ID,
            sub="cognito-sub-0001",
        )

        verified = security._extract_and_verify_bearer_token(f"Bearer {token}")
        user_id = security.get_current_user_id(verified)

        assert user_id == "cognito-sub-0001"

    def test_legacy_fe_client_token_raises_401(self, signing_key_patch):
        from fastapi import HTTPException

        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id=_LEGACY_FE_CLIENT_ID,
            sub="cognito-sub-0002",
        )

        with pytest.raises(HTTPException) as exc_info:
            security._extract_and_verify_bearer_token(f"Bearer {token}")

        assert exc_info.value.status_code == 401

    def test_unknown_client_token_raises_401(self, signing_key_patch):
        from fastapi import HTTPException

        token = _make_token(
            signing_key_patch,
            token_use="access",
            client_id="some-attacker-controlled-client-id",
        )

        with pytest.raises(HTTPException) as exc_info:
            security._extract_and_verify_bearer_token(f"Bearer {token}")

        assert exc_info.value.status_code == 401


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
