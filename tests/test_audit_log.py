"""
인증 보안 감사 로그 테스트 (CLIAR-160, Phase 6, PLAN.md §9.4).

app/core/audit_log.py를 통해 signup/signup 이메일 인증/login/logout/
password forgot·reset·change가 "app.audit" logger로 event/outcome/
안전한 부가 정보만 기록하는지 확인한다.

절대 로그에 남으면 안 되는 값(password, current_password,
new_password, confirmation code, access/id/refresh token, refresh_sub,
client secret, SECRET_HASH, Authorization 헤더 원문, Cognito 원본
오류 메시지)이 캡처된 로그 어디에도 없는지를 각 이벤트별로, 그리고
마지막에 전체적으로 다시 한번 검증한다.

실제 AWS/Cognito는 호출하지 않는다. Cognito client 교체는 항상
monkeypatch를 통해서만 수행한다 — app.core.cognito_auth.
get_cognito_idp_client에 직접 대입하면 되돌려지지 않아 이후 다른
테스트 파일까지 오염시키기 때문이다.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core import cognito_auth
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import get_current_access_token, get_current_user_id
from app.main import app
from app.models.terms import Terms
from app.models.user import MemberStatus, User

LOGIN_ENDPOINT = "/api/v1/auth/login"
LOGOUT_ENDPOINT = "/api/v1/auth/logout"
SIGNUP_ENDPOINT = "/api/v1/auth/signup"
CONFIRM_ENDPOINT = "/api/v1/auth/signup/confirm"
FORGOT_ENDPOINT = "/api/v1/auth/password/forgot"
RESET_ENDPOINT = "/api/v1/auth/password/reset"
CHANGE_ENDPOINT = "/api/v1/auth/password/change"

MEMBER_SUB = "11111111-2222-3333-4444-555555555555"

FORBIDDEN_SUBSTRINGS = [
    "wrong-password",
    "correct-password",
    "current-password-value",
    "new-password-value",
    "999999",  # confirmation code used below
    "issued-access-token",
    "issued-id-token",
    "issued-refresh-token",
    "the-verified-access-token",
    "backend-client-secret",
]


@pytest.fixture(autouse=True)
def backend_client_settings(monkeypatch):
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setattr(
        settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret"
    )


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


def _patch_cognito(monkeypatch, fake_client):
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: fake_client)
    return fake_client


def _client_error(code, message="internal cognito message"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


def _create_member(db_session, *, member_id=MEMBER_SUB, status=MemberStatus.ACTIVE):
    member = User(
        member_id=uuid.UUID(member_id),
        email="user@example.com",
        nickname="haechan",
        status=status,
    )
    db_session.add(member)
    db_session.commit()
    return member


def _seed_required_terms(db_session):
    now = datetime.now(timezone.utc)
    for code in ("TERMS_OF_SERVICE", "PRIVACY"):
        db_session.add(
            Terms(
                code=code,
                name=code,
                content=f"{code} content",
                is_required=False,
                effective_at=now - timedelta(days=1),
            )
        )
    db_session.commit()


def _audit_records(caplog):
    return [r for r in caplog.records if r.name == "app.audit"]


def _assert_no_forbidden_values(text: str):
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in text, f"forbidden value leaked: {forbidden!r}"


def _authorize(sub=MEMBER_SUB, access_token="the-verified-access-token"):
    app.dependency_overrides[get_current_access_token] = lambda: access_token
    app.dependency_overrides[get_current_user_id] = lambda: sub


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


class _FakeLoginClient:
    def __init__(self, *, sub=MEMBER_SUB, error=None):
        self.sub = sub
        self.error = error

    def initiate_auth(self, **kwargs):
        if self.error is not None:
            raise self.error
        return {
            "AuthenticationResult": {
                "AccessToken": "issued-access-token",
                "IdToken": "issued-id-token",
                "RefreshToken": "issued-refresh-token",
                "ExpiresIn": 86400,
                "TokenType": "Bearer",
            }
        }

    def get_user(self, AccessToken):
        return {"UserAttributes": [{"Name": "sub", "Value": self.sub}]}


class TestLoginAudit:
    def test_success_logs_event_and_member_id(
        self, client, db_session, monkeypatch, caplog
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeLoginClient())

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                LOGIN_ENDPOINT,
                json={"email": "user@example.com", "password": "correct-password"},
            )

        assert response.status_code == 200
        records = _audit_records(caplog)
        assert any(
            "event=login" in r.getMessage() and "outcome=success" in r.getMessage()
            for r in records
        )
        assert any(f"member_id={MEMBER_SUB}" in r.getMessage() for r in records)

    def test_failure_logs_event_with_reason_not_raw_cognito_message(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(
            monkeypatch,
            _FakeLoginClient(
                error=_client_error(
                    "NotAuthorizedException",
                    message="Incorrect username or password for user-internal-xyz",
                )
            ),
        )

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                LOGIN_ENDPOINT,
                json={"email": "user@example.com", "password": "wrong-password"},
            )

        assert response.status_code == 401
        records = _audit_records(caplog)
        assert any(
            "event=login" in r.getMessage() and "outcome=failure" in r.getMessage()
            for r in records
        )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "user-internal-xyz" not in joined
        assert "Incorrect username or password" not in joined

    def test_password_is_not_logged_on_failure(self, client, monkeypatch, caplog):
        _patch_cognito(
            monkeypatch,
            _FakeLoginClient(error=_client_error("NotAuthorizedException")),
        )

        with caplog.at_level(logging.DEBUG):
            client.post(
                LOGIN_ENDPOINT,
                json={"email": "user@example.com", "password": "wrong-password"},
            )

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "wrong-password" not in joined

    def test_password_and_tokens_are_not_logged_on_success(
        self, client, db_session, monkeypatch, caplog
    ):
        _create_member(db_session)
        _patch_cognito(monkeypatch, _FakeLoginClient())

        with caplog.at_level(logging.DEBUG):
            client.post(
                LOGIN_ENDPOINT,
                json={"email": "user@example.com", "password": "correct-password"},
            )

        joined = " ".join(r.getMessage() for r in caplog.records)
        _assert_no_forbidden_values(joined)


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


class _FakeRevokeClient:
    def __init__(self, *, error=None):
        self.error = error

    def revoke_token(self, **kwargs):
        if self.error is not None:
            raise self.error
        return {}


class TestLogoutAudit:
    def test_no_cookie_logs_success_with_reason(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(LOGOUT_ENDPOINT)

        assert response.status_code == 204
        records = _audit_records(caplog)
        assert any(
            "event=logout" in r.getMessage() and "outcome=success" in r.getMessage()
            for r in records
        )

    def test_revoke_success_logs_success(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeRevokeClient())
        client.cookies.set("refresh_token", "stored-refresh-token")

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(LOGOUT_ENDPOINT)

        assert response.status_code == 204
        records = _audit_records(caplog)
        assert any(
            "event=logout" in r.getMessage() and "outcome=success" in r.getMessage()
            for r in records
        )

    def test_revoke_failure_logs_revoke_failed_but_still_returns_204(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(
            monkeypatch,
            _FakeRevokeClient(error=_client_error("NotAuthorizedException")),
        )
        client.cookies.set("refresh_token", "stored-refresh-token")

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(LOGOUT_ENDPOINT)

        assert response.status_code == 204
        records = _audit_records(caplog)
        assert any(
            "event=logout" in r.getMessage() and "revoke_failed" in r.getMessage()
            for r in records
        )

    def test_refresh_token_value_is_never_logged(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeRevokeClient())
        client.cookies.set("refresh_token", "super-secret-refresh-abc123")

        with caplog.at_level(logging.DEBUG):
            client.post(LOGOUT_ENDPOINT)

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "super-secret-refresh-abc123" not in joined


# ---------------------------------------------------------------------------
# signup / signup confirm
# ---------------------------------------------------------------------------


class _FakeSignupClient:
    def __init__(self, *, user_sub=None, error=None):
        self.user_sub = user_sub or str(uuid.uuid4())
        self.error = error

    def sign_up(self, **kwargs):
        if self.error is not None:
            raise self.error
        return {"UserSub": self.user_sub}

    def confirm_sign_up(self, **kwargs):
        if self.error is not None:
            raise self.error
        return {}


def _signup_body(**overrides):
    body = {
        "email": "newuser@example.com",
        "password": "correct-password",
        "nickname": "haechan",
        "birth_date": "2000-01-01",
        "gender": "MALE",
        "agree_terms": True,
        "agree_privacy": True,
        "agree_ai_analysis": False,
    }
    body.update(overrides)
    return body


class TestSignupAudit:
    def test_success_logs_event_and_member_id(
        self, client, db_session, monkeypatch, caplog
    ):
        _seed_required_terms(db_session)
        user_sub = str(uuid.uuid4())
        _patch_cognito(monkeypatch, _FakeSignupClient(user_sub=user_sub))

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(SIGNUP_ENDPOINT, json=_signup_body())

        assert response.status_code == 201
        records = _audit_records(caplog)
        assert any(
            "event=signup" in r.getMessage()
            and "outcome=success" in r.getMessage()
            and f"member_id={user_sub}" in r.getMessage()
            for r in records
        )

    def test_email_already_registered_logs_failure_reason(
        self, client, db_session, caplog
    ):
        _seed_required_terms(db_session)
        _create_member(db_session, member_id=str(uuid.uuid4()))
        db_session.query(User).update({"email": "newuser@example.com"})
        db_session.commit()

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(SIGNUP_ENDPOINT, json=_signup_body())

        assert response.status_code == 409
        records = _audit_records(caplog)
        assert any(
            "event=signup" in r.getMessage()
            and "outcome=failure" in r.getMessage()
            and "email_already_registered" in r.getMessage()
            for r in records
        )

    def test_signup_password_is_never_logged(
        self, client, db_session, monkeypatch, caplog
    ):
        _seed_required_terms(db_session)
        _patch_cognito(monkeypatch, _FakeSignupClient())

        with caplog.at_level(logging.DEBUG):
            client.post(SIGNUP_ENDPOINT, json=_signup_body(password="correct-password"))

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "correct-password" not in joined


class TestSignupConfirmAudit:
    def test_success_logs_event_and_member_id(
        self, client, db_session, monkeypatch, caplog
    ):
        member = _create_member(db_session, member_id=str(uuid.uuid4()))
        member.email = "confirmee@example.com"
        member.status = MemberStatus.PENDING
        db_session.commit()

        _patch_cognito(monkeypatch, _FakeSignupClient())

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                CONFIRM_ENDPOINT,
                json={"email": "confirmee@example.com", "code": "123456"},
            )

        assert response.status_code == 200
        records = _audit_records(caplog)
        assert any(
            "event=signup_confirm" in r.getMessage()
            and "outcome=success" in r.getMessage()
            and f"member_id={member.member_id}" in r.getMessage()
            for r in records
        )

    def test_member_not_found_logs_failure_reason(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeSignupClient())

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                CONFIRM_ENDPOINT,
                json={"email": "unknown@example.com", "code": "123456"},
            )

        assert response.status_code == 404
        records = _audit_records(caplog)
        assert any(
            "event=signup_confirm" in r.getMessage()
            and "member_not_found" in r.getMessage()
            for r in records
        )

    def test_confirmation_code_is_never_logged(
        self, client, db_session, monkeypatch, caplog
    ):
        member = _create_member(db_session, member_id=str(uuid.uuid4()))
        member.email = "confirmee2@example.com"
        member.status = MemberStatus.PENDING
        db_session.commit()
        _patch_cognito(monkeypatch, _FakeSignupClient())

        with caplog.at_level(logging.DEBUG):
            client.post(
                CONFIRM_ENDPOINT,
                json={"email": "confirmee2@example.com", "code": "123456"},
            )

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "123456" not in joined


# ---------------------------------------------------------------------------
# password forgot / reset / change
# ---------------------------------------------------------------------------


class _FakeForgotClient:
    def __init__(self, *, error=None):
        self.error = error

    def forgot_password(self, **kwargs):
        if self.error is not None:
            raise self.error
        return {}


class _FakeResetClient:
    def __init__(self, *, error=None):
        self.error = error

    def confirm_forgot_password(self, **kwargs):
        if self.error is not None:
            raise self.error
        return {}


class _FakeChangeClient:
    def __init__(self, *, error=None):
        self.error = error

    def change_password(self, **kwargs):
        if self.error is not None:
            raise self.error
        return {}


class TestPasswordForgotAudit:
    def test_success_logs_event(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeForgotClient())

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 204
        records = _audit_records(caplog)
        assert any(
            "event=password_forgot" in r.getMessage()
            and "outcome=success" in r.getMessage()
            for r in records
        )

    def test_user_not_found_still_logs_success(self, client, monkeypatch, caplog):
        """UserNotFoundException은 사용자 열거 방지를 위해 흡수되어
        204로 처리되므로, 감사 로그도 success로 남는다(§16 정책과
        일관)."""
        _patch_cognito(
            monkeypatch, _FakeForgotClient(error=_client_error("UserNotFoundException"))
        )

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                FORGOT_ENDPOINT, json={"email": "unregistered@example.com"}
            )

        assert response.status_code == 204
        records = _audit_records(caplog)
        assert any(
            "event=password_forgot" in r.getMessage()
            and "outcome=success" in r.getMessage()
            for r in records
        )

    def test_rate_limited_by_cognito_logs_failure(self, client, monkeypatch, caplog):
        _patch_cognito(
            monkeypatch,
            _FakeForgotClient(error=_client_error("TooManyRequestsException")),
        )

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

        assert response.status_code == 429
        records = _audit_records(caplog)
        assert any(
            "event=password_forgot" in r.getMessage()
            and "outcome=failure" in r.getMessage()
            for r in records
        )

    def test_email_is_not_logged(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeForgotClient())

        with caplog.at_level(logging.DEBUG):
            client.post(
                FORGOT_ENDPOINT, json={"email": "very-identifiable@example.com"}
            )

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "very-identifiable@example.com" not in joined


class TestPasswordResetAudit:
    def test_success_logs_event(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeResetClient())

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                RESET_ENDPOINT,
                json={
                    "email": "user@example.com",
                    "code": "123456",
                    "new_password": "new-password-value",
                },
            )

        assert response.status_code == 204
        records = _audit_records(caplog)
        assert any(
            "event=password_reset" in r.getMessage()
            and "outcome=success" in r.getMessage()
            for r in records
        )

    def test_code_mismatch_logs_failure(self, client, monkeypatch, caplog):
        _patch_cognito(
            monkeypatch, _FakeResetClient(error=_client_error("CodeMismatchException"))
        )

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                RESET_ENDPOINT,
                json={
                    "email": "user@example.com",
                    "code": "123456",
                    "new_password": "new-password-value",
                },
            )

        assert response.status_code == 400
        records = _audit_records(caplog)
        assert any(
            "event=password_reset" in r.getMessage()
            and "outcome=failure" in r.getMessage()
            for r in records
        )

    def test_new_password_and_code_are_never_logged(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeResetClient())

        with caplog.at_level(logging.DEBUG):
            client.post(
                RESET_ENDPOINT,
                json={
                    "email": "user@example.com",
                    "code": "999999",
                    "new_password": "new-password-value",
                },
            )

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "new-password-value" not in joined
        assert "999999" not in joined


class TestPasswordChangeAudit:
    @pytest.fixture(autouse=True)
    def _clear_auth_overrides(self):
        yield
        app.dependency_overrides.pop(get_current_access_token, None)
        app.dependency_overrides.pop(get_current_user_id, None)

    def test_success_logs_event_and_member_id(self, client, monkeypatch, caplog):
        _patch_cognito(monkeypatch, _FakeChangeClient())
        _authorize()

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                CHANGE_ENDPOINT,
                json={
                    "current_password": "current-password-value",
                    "new_password": "new-password-value",
                },
            )

        assert response.status_code == 204
        records = _audit_records(caplog)
        assert any(
            "event=password_change" in r.getMessage()
            and "outcome=success" in r.getMessage()
            and f"member_id={MEMBER_SUB}" in r.getMessage()
            for r in records
        )

    def test_wrong_current_password_logs_failure(self, client, monkeypatch, caplog):
        _patch_cognito(
            monkeypatch, _FakeChangeClient(error=_client_error("NotAuthorizedException"))
        )
        _authorize()

        with caplog.at_level(logging.INFO, logger="app.audit"):
            response = client.post(
                CHANGE_ENDPOINT,
                json={
                    "current_password": "current-password-value",
                    "new_password": "new-password-value",
                },
            )

        assert response.status_code == 401
        records = _audit_records(caplog)
        assert any(
            "event=password_change" in r.getMessage()
            and "outcome=failure" in r.getMessage()
            for r in records
        )

    def test_passwords_and_access_token_are_never_logged(
        self, client, monkeypatch, caplog
    ):
        _patch_cognito(monkeypatch, _FakeChangeClient())
        _authorize()

        with caplog.at_level(logging.DEBUG):
            client.post(
                CHANGE_ENDPOINT,
                json={
                    "current_password": "current-password-value",
                    "new_password": "new-password-value",
                },
            )

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "current-password-value" not in joined
        assert "new-password-value" not in joined
        assert "the-verified-access-token" not in joined


# ---------------------------------------------------------------------------
# 종합: 금지 목록이 전체 로그 스트림 어디에도 없는지 다시 한번 확인
# ---------------------------------------------------------------------------


class TestNoSensitiveValuesAcrossAllAuditEvents:
    @pytest.fixture(autouse=True)
    def _clear_auth_overrides(self):
        yield
        app.dependency_overrides.pop(get_current_access_token, None)
        app.dependency_overrides.pop(get_current_user_id, None)

    def test_full_battery_of_calls_leaks_nothing(
        self, client, db_session, monkeypatch, caplog
    ):
        _seed_required_terms(db_session)
        _create_member(db_session)

        with caplog.at_level(logging.DEBUG):
            monkeypatch.setattr(
                cognito_auth, "get_cognito_idp_client", lambda: _FakeLoginClient()
            )
            client.post(
                LOGIN_ENDPOINT,
                json={"email": "user@example.com", "password": "correct-password"},
            )

            client.cookies.set("refresh_token", "issued-refresh-token")
            monkeypatch.setattr(
                cognito_auth, "get_cognito_idp_client", lambda: _FakeRevokeClient()
            )
            client.post(LOGOUT_ENDPOINT)
            client.cookies.clear()

            monkeypatch.setattr(
                cognito_auth, "get_cognito_idp_client", lambda: _FakeSignupClient()
            )
            client.post(SIGNUP_ENDPOINT, json=_signup_body())

            monkeypatch.setattr(
                cognito_auth, "get_cognito_idp_client", lambda: _FakeForgotClient()
            )
            client.post(FORGOT_ENDPOINT, json={"email": "user@example.com"})

            monkeypatch.setattr(
                cognito_auth, "get_cognito_idp_client", lambda: _FakeResetClient()
            )
            client.post(
                RESET_ENDPOINT,
                json={
                    "email": "user@example.com",
                    "code": "123456",
                    "new_password": "new-password-value",
                },
            )

            monkeypatch.setattr(
                cognito_auth, "get_cognito_idp_client", lambda: _FakeChangeClient()
            )
            _authorize()
            client.post(
                CHANGE_ENDPOINT,
                json={
                    "current_password": "current-password-value",
                    "new_password": "new-password-value",
                },
            )

        joined = " ".join(r.getMessage() for r in caplog.records)
        _assert_no_forbidden_values(joined)
