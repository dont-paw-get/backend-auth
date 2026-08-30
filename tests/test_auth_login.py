"""
POST /api/v1/auth/login 테스트 (CLIAR-153, Phase 4).

BE 주도 로그인: backend-auth가 Cognito InitiateAuth
(USER_PASSWORD_AUTH)를 직접 호출하고, 발급된 Access Token으로
GetUser를 호출해 sub를 확보한 뒤 그 sub로 member를 조회한다.

실제 AWS/Cognito에는 접속하지 않는다. app.core.cognito_auth의
get_cognito_idp_client를 monkeypatch해 boto3 client를 대체한다
(tests/test_auth_signup.py와 동일한 패턴).

이 endpoint는 Bearer Access Token 인증을 요구하지 않는다(아직
로그인하지 않은 사용자가 호출하는 API이기 때문).
"""

import logging
import uuid
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core import cognito_auth
from app.core.cognito_auth import secret_hash
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.user import MemberStatus, User

LOGIN_ENDPOINT = "/api/v1/auth/login"

MEMBER_SUB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(engine):
    session = Session(bind=engine)
    yield session
    session.close()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
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


class _FakeCognitoClient:
    """
    InitiateAuth(USER_PASSWORD_AUTH) + GetUser를 흉내내는 fake Cognito
    client. 테스트별로 필요한 만큼만 override해서 사용한다.
    """

    def __init__(
        self,
        *,
        sub=MEMBER_SUB,
        initiate_error=None,
        get_user_error=None,
        auth_result=None,
        response_override=None,
    ):
        self.sub = sub
        self.initiate_error = initiate_error
        self.get_user_error = get_user_error
        self.response_override = response_override
        self.auth_result = auth_result or {
            "AccessToken": "issued-access-token",
            "IdToken": "issued-id-token",
            "RefreshToken": "issued-refresh-token",
            "ExpiresIn": 86400,
            "TokenType": "Bearer",
        }
        self.initiate_auth_calls = []
        self.get_user_calls = []

    def initiate_auth(self, **kwargs):
        self.initiate_auth_calls.append(kwargs)
        if self.initiate_error is not None:
            raise self.initiate_error
        if self.response_override is not None:
            return self.response_override
        return {"AuthenticationResult": dict(self.auth_result)}

    def get_user(self, AccessToken):
        self.get_user_calls.append(AccessToken)
        if self.get_user_error is not None:
            raise self.get_user_error
        return {
            "Username": self.sub,
            "UserAttributes": [
                {"Name": "sub", "Value": self.sub},
                {"Name": "email", "Value": "user@example.com"},
            ],
        }


def _patch_cognito(monkeypatch, fake_client):
    monkeypatch.setattr(
        cognito_auth, "get_cognito_idp_client", lambda: fake_client
    )
    return fake_client


def _client_error(code, message="cognito message"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "InitiateAuth"
    )


def _create_member(
    db_session,
    *,
    member_id=MEMBER_SUB,
    email="user@example.com",
    nickname="haechan",
    status=MemberStatus.ACTIVE,
    deleted_at=None,
):
    member = User(
        member_id=uuid.UUID(member_id),
        email=email,
        nickname=nickname,
        status=status,
        deleted_at=deleted_at,
    )
    db_session.add(member)
    db_session.commit()
    return member


def _login_body(**overrides):
    body = {"email": "user@example.com", "password": "P@ssw0rd123!"}
    body.update(overrides)
    return body


def _set_cookie_headers(response):
    return response.headers.get_list("set-cookie")


def _cookie_header(response, name):
    for header in _set_cookie_headers(response):
        if header.startswith(f"{name}="):
            return header
    return None


class TestLoginSuccess:
    def test_active_member_login_returns_200(self, client, db_session, monkeypatch):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 200

    def test_response_contains_access_and_id_token(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        body = client.post(LOGIN_ENDPOINT, json=_login_body()).json()

        assert body["access_token"] == "issued-access-token"
        assert body["id_token"] == "issued-id-token"

    def test_response_contains_expires_in_and_token_type(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        body = client.post(LOGIN_ENDPOINT, json=_login_body()).json()

        assert body["expires_in"] == 86400
        assert body["token_type"] == "Bearer"

    def test_response_body_does_not_contain_refresh_token(
        self, client, db_session, monkeypatch
    ):
        """Refresh Token은 HttpOnly 쿠키로만 전달한다(PLAN.md D3)."""
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert "refresh_token" not in response.json()
        assert "issued-refresh-token" not in response.text

    def test_response_contains_member(self, client, db_session, monkeypatch):
        _create_member(db_session, nickname="haechan")
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        member = client.post(LOGIN_ENDPOINT, json=_login_body()).json()["member"]

        assert member["member_id"] == MEMBER_SUB
        assert member["email"] == "user@example.com"
        assert member["nickname"] == "haechan"
        assert member["status"] == "ACTIVE"

    def test_member_is_looked_up_by_cognito_sub_not_by_request_email(
        self, client, db_session, monkeypatch
    ):
        """
        member 조회 키는 request body의 email이 아니라 Cognito가
        반환한 sub다. email이 다른 member row가 있어도 sub로 찾은
        row가 반환되어야 한다.
        """
        _create_member(
            db_session, email="real-owner@example.com", nickname="owner"
        )
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        member = client.post(LOGIN_ENDPOINT, json=_login_body()).json()["member"]

        assert member["member_id"] == MEMBER_SUB
        assert member["email"] == "real-owner@example.com"

    def test_sub_is_obtained_from_cognito_get_user_with_issued_access_token(
        self, client, db_session, monkeypatch
    ):
        """
        토큰 문자열을 서명 검증 없이 파싱하지 않고, 방금 발급받은
        Access Token으로 Cognito GetUser를 호출해 sub를 얻는다.
        """
        _create_member(db_session)
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())

        client.post(LOGIN_ENDPOINT, json=_login_body())

        assert fake.get_user_calls == ["issued-access-token"]


class TestLoginCognitoCall:
    def test_uses_user_password_auth_flow(self, client, db_session, monkeypatch):
        _create_member(db_session)
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())

        client.post(LOGIN_ENDPOINT, json=_login_body())

        assert fake.initiate_auth_calls[0]["AuthFlow"] == "USER_PASSWORD_AUTH"

    def test_uses_backend_app_client_id(self, client, db_session, monkeypatch):
        _create_member(db_session)
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())

        client.post(LOGIN_ENDPOINT, json=_login_body())

        assert (
            fake.initiate_auth_calls[0]["ClientId"]
            == settings.COGNITO_BACKEND_CLIENT_ID
        )

    def test_secret_hash_username_is_the_normalized_email(
        self, client, db_session, monkeypatch
    ):
        """USER_PASSWORD_AUTH의 SECRET_HASH는 USERNAME(=email)으로
        계산되어야 한다."""
        _create_member(db_session)
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())

        client.post(LOGIN_ENDPOINT, json=_login_body())

        parameters = fake.initiate_auth_calls[0]["AuthParameters"]
        assert parameters["USERNAME"] == "user@example.com"
        assert parameters["SECRET_HASH"] == secret_hash("user@example.com")

    def test_password_is_forwarded_to_cognito(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())

        client.post(LOGIN_ENDPOINT, json=_login_body(password="Str0ng!Pass"))

        assert fake.initiate_auth_calls[0]["AuthParameters"]["PASSWORD"] == (
            "Str0ng!Pass"
        )

    def test_email_is_normalized_before_calling_cognito(
        self, client, db_session, monkeypatch
    ):
        """signup/availability와 동일하게 strip + lower 정규화를 적용한다."""
        _create_member(db_session)
        fake = _patch_cognito(monkeypatch, _FakeCognitoClient())

        client.post(LOGIN_ENDPOINT, json=_login_body(email="  User@Example.COM  "))

        parameters = fake.initiate_auth_calls[0]["AuthParameters"]
        assert parameters["USERNAME"] == "user@example.com"
        assert parameters["SECRET_HASH"] == secret_hash("user@example.com")


class TestLoginCookies:
    def test_sets_refresh_token_http_only_cookie(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        header = _cookie_header(response, "refresh_token")
        assert header is not None
        assert "issued-refresh-token" in header
        assert "HttpOnly" in header

    def test_sets_refresh_sub_http_only_cookie_with_cognito_sub(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        header = _cookie_header(response, "refresh_sub")
        assert header is not None
        assert MEMBER_SUB in header
        assert "HttpOnly" in header

    def test_cookies_use_configured_security_attributes(
        self, client, db_session, monkeypatch
    ):
        """Secure/SameSite/Path는 하드코딩하지 않고 settings를 따른다."""
        monkeypatch.setattr(settings, "COOKIE_SECURE", True)
        monkeypatch.setattr(settings, "COOKIE_SAMESITE", "lax")
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        for name in ("refresh_token", "refresh_sub"):
            header = _cookie_header(response, name)
            assert "Secure" in header
            assert "SameSite=lax" in header
            assert "Path=/api/v1/auth" in header

    def test_cookie_secure_flag_follows_settings_for_dev_http(
        self, client, db_session, monkeypatch
    ):
        """DEV(http) 환경을 위해 COOKIE_SECURE=False도 반영되어야 한다."""
        monkeypatch.setattr(settings, "COOKIE_SECURE", False)
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert "Secure" not in _cookie_header(response, "refresh_token")

    def test_no_cookies_are_set_when_login_fails(
        self, client, db_session, monkeypatch
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=_client_error("NotAuthorizedException")
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 401
        assert _set_cookie_headers(response) == []


class TestLoginMemberStatus:
    def test_pending_member_returns_403_email_not_verified(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session, status=MemberStatus.PENDING)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "EMAIL_NOT_VERIFIED"

    def test_withdrawn_member_returns_403(self, client, db_session, monkeypatch):
        _create_member(db_session, status=MemberStatus.WITHDRAWN)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 403
        assert response.json()["detail"] == "This member has been withdrawn"

    def test_member_with_deleted_at_returns_403(
        self, client, db_session, monkeypatch
    ):
        _create_member(
            db_session,
            status=MemberStatus.ACTIVE,
            deleted_at=datetime.now(timezone.utc),
        )
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 403

    def test_withdrawn_takes_precedence_over_pending(
        self, client, db_session, monkeypatch
    ):
        """탈퇴는 종착 상태이므로 PENDING보다 먼저 판정한다
        (app/api/deps.py의 get_current_member와 동일한 순서)."""
        _create_member(
            db_session,
            status=MemberStatus.PENDING,
            deleted_at=datetime.now(timezone.utc),
        )
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 403
        assert response.json()["detail"] == "This member has been withdrawn"

    def test_missing_member_row_returns_404(self, client, monkeypatch):
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 404

    def test_no_cookies_are_set_when_member_status_blocks_login(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session, status=MemberStatus.PENDING)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert _set_cookie_headers(response) == []


class TestLoginCognitoErrors:
    def test_wrong_password_returns_401(self, client, db_session, monkeypatch):
        _create_member(db_session)
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=_client_error("NotAuthorizedException")
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body(password="wrong!"))

        assert response.status_code == 401
        assert response.json()["detail"] == "이메일 또는 비밀번호가 올바르지 않습니다"

    def test_user_not_found_returns_the_same_401_response(
        self, client, monkeypatch
    ):
        """user enumeration 방지: NotAuthorizedException과
        UserNotFoundException의 응답이 완전히 동일해야 한다."""
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=_client_error("NotAuthorizedException")
            ),
        )
        not_authorized = client.post(LOGIN_ENDPOINT, json=_login_body())

        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=_client_error("UserNotFoundException")
            ),
        )
        user_not_found = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert not_authorized.status_code == user_not_found.status_code == 401
        assert not_authorized.json() == user_not_found.json()

    def test_user_not_confirmed_returns_403_email_not_verified(
        self, client, monkeypatch
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=_client_error("UserNotConfirmedException")
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "EMAIL_NOT_VERIFIED"

    @pytest.mark.parametrize(
        "error_code",
        [
            "TooManyRequestsException",
            "LimitExceededException",
            "TooManyFailedAttemptsException",
        ],
    )
    def test_rate_limit_errors_return_429(self, client, monkeypatch, error_code):
        _patch_cognito(
            monkeypatch, _FakeCognitoClient(initiate_error=_client_error(error_code))
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 429

    def test_unexpected_client_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(initiate_error=_client_error("InternalErrorException")),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 502

    def test_connection_error_returns_502(self, client, monkeypatch):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=EndpointConnectionError(
                    endpoint_url="https://cognito-idp.example.com"
                )
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 502

    def test_get_user_failure_is_mapped_through_the_same_table(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                get_user_error=_client_error("InternalErrorException")
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 502

    def test_missing_refresh_token_in_auth_result_returns_502(
        self, client, db_session, monkeypatch
    ):
        """
        최종 로그인 계약(PLAN.md D3)은 refresh_token을 HttpOnly
        쿠키로 반드시 내려준다. AccessToken은 있지만
        AuthenticationResult에 RefreshToken이 없다면 반쪽짜리 세션을
        성공으로 위장하지 않고 502로 실패시켜야 한다.
        """
        _create_member(db_session)
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                auth_result={
                    "AccessToken": "issued-access-token",
                    "IdToken": "issued-id-token",
                    "ExpiresIn": 86400,
                    "TokenType": "Bearer",
                    # RefreshToken 누락
                }
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 502

    def test_missing_refresh_token_sets_no_cookies(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                auth_result={
                    "AccessToken": "issued-access-token",
                    "ExpiresIn": 86400,
                    "TokenType": "Bearer",
                }
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert _set_cookie_headers(response) == []

    def test_missing_refresh_token_does_not_leak_cognito_internals_or_tokens(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                auth_result={
                    "AccessToken": "issued-access-token",
                    "ExpiresIn": 86400,
                    "TokenType": "Bearer",
                }
            ),
        )

        secret_password = "super-secret-password-abc123"
        response = client.post(
            LOGIN_ENDPOINT, json=_login_body(password=secret_password)
        )

        assert "issued-access-token" not in response.text
        assert secret_password not in response.text

    def test_unsupported_auth_challenge_returns_502(self, client, monkeypatch):
        """MFA/NEW_PASSWORD_REQUIRED 등 챌린지는 범위 밖이므로
        성공으로 위장하지 않는다(PLAN.md §15)."""
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                response_override={
                    "ChallengeName": "NEW_PASSWORD_REQUIRED",
                    "Session": "challenge-session",
                }
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 502

    def test_error_response_does_not_leak_cognito_message(
        self, client, monkeypatch
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=_client_error(
                    "NotAuthorizedException",
                    message="Incorrect username or password for internal-id-xyz",
                )
            ),
        )

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert "internal-id-xyz" not in response.text


class TestLoginSecrecy:
    def test_password_is_never_echoed_in_the_response(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        secret_password = "super-secret-password-abc123"
        response = client.post(
            LOGIN_ENDPOINT, json=_login_body(password=secret_password)
        )

        assert secret_password not in response.text

    def test_password_is_not_echoed_in_validation_errors(self, client):
        secret_password = "super-secret-password-abc123"

        response = client.post(
            LOGIN_ENDPOINT, json={"email": "  ", "password": secret_password}
        )

        assert response.status_code == 422
        assert secret_password not in response.text

    def test_password_and_tokens_are_not_logged(
        self, client, db_session, monkeypatch, caplog
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        secret_password = "super-secret-password-abc123"
        with caplog.at_level(logging.DEBUG):
            client.post(LOGIN_ENDPOINT, json=_login_body(password=secret_password))

        logged = caplog.text
        assert secret_password not in logged
        assert "issued-access-token" not in logged
        assert "issued-refresh-token" not in logged
        assert "issued-id-token" not in logged

    def test_failed_login_does_not_log_the_password(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(
            monkeypatch,
            _FakeCognitoClient(
                initiate_error=_client_error("NotAuthorizedException")
            ),
        )

        secret_password = "super-secret-password-abc123"
        with caplog.at_level(logging.DEBUG):
            client.post(LOGIN_ENDPOINT, json=_login_body(password=secret_password))

        assert secret_password not in caplog.text


class TestLoginValidation:
    def test_missing_password_returns_422(self, client):
        response = client.post(LOGIN_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 422

    def test_blank_email_returns_422(self, client):
        response = client.post(
            LOGIN_ENDPOINT, json={"email": "   ", "password": "P@ssw0rd123!"}
        )

        assert response.status_code == 422

    def test_blank_password_returns_422(self, client):
        response = client.post(
            LOGIN_ENDPOINT, json={"email": "user@example.com", "password": "   "}
        )

        assert response.status_code == 422

    def test_unexpected_field_returns_422(self, client):
        response = client.post(
            LOGIN_ENDPOINT, json=_login_body(member_id="attacker-supplied")
        )

        assert response.status_code == 422

    def test_does_not_require_authorization_header(
        self, client, db_session, monkeypatch
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeCognitoClient())

        response = client.post(LOGIN_ENDPOINT, json=_login_body())

        assert response.status_code == 200
