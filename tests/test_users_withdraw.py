"""
DELETE /api/v1/users/me 테스트 (CLIAR-113)

Cognito 연동 회원탈퇴 API 검증. 실제 AWS Cognito에 접속하지 않기 위해
app.core.cognito.verify_cognito_token / delete_cognito_user를
monkeypatch한다.

탈퇴 처리 순서:
  1. status: ACTIVE -> WITHDRAWN (commit)
  2. Cognito DeleteUser(access token 기반)
  3. 성공 시 deleted_at 기록 (commit) -> 204

재시도/멱등성:
  - status=WITHDRAWN, deleted_at=NULL: 재시도 허용(Cognito부터 다시 시도)
  - status=WITHDRAWN, deleted_at!=NULL: 추가 변경 없이 204
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import users as users_api
from app.core import security
from app.core.database import Base, get_db
from app.main import app
from app.models.user import MemberStatus, User


ENDPOINT = "/api/v1/users/me"


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


def _create_member(
    db_session,
    member_id=None,
    email="withdraw-target@example.com",
    nickname="withdraw-nick",
    status=MemberStatus.ACTIVE,
    deleted_at=None,
):
    member = User(
        member_id=member_id or uuid.uuid4(),
        email=email,
        nickname=nickname,
        status=status,
        deleted_at=deleted_at,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _authenticate_as(monkeypatch, sub, token="fake-access-token"):
    """DELETE /me 인증에 필요한 verify_cognito_token만 patch한다.
    (GetUser는 DELETE 경로에서 호출되지 않는다.)"""

    def _fake_verify(received_token):
        assert received_token == token
        return {"sub": sub, "token_use": "access"}

    monkeypatch.setattr(security, "verify_cognito_token", _fake_verify)
    return {"Authorization": f"Bearer {token}"}


def _patch_delete_cognito_user(monkeypatch, fn):
    """app/api/users.py가 import한 이름(delete_cognito_user)을 patch한다."""
    monkeypatch.setattr(users_api, "delete_cognito_user", fn)


class TestWithdrawSuccess:
    def test_active_member_withdrawal_returns_204(self, client, db_session, monkeypatch):
        member = _create_member(db_session)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))
        _patch_delete_cognito_user(monkeypatch, lambda access_token, *, sub: None)

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 204
        assert response.content == b""

    def test_status_changes_active_to_withdrawn(self, client, db_session, monkeypatch):
        member = _create_member(db_session)
        member_id = member.member_id
        headers = _authenticate_as(monkeypatch, sub=str(member_id))
        _patch_delete_cognito_user(monkeypatch, lambda access_token, *, sub: None)

        client.delete(ENDPOINT, headers=headers)

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.member_id == member_id).one()
        assert refreshed.status == MemberStatus.WITHDRAWN

    def test_cognito_delete_user_receives_access_token(self, client, db_session, monkeypatch):
        member = _create_member(db_session)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))

        received = {}

        def _fake_delete(access_token, *, sub):
            received["access_token"] = access_token
            received["sub"] = sub

        _patch_delete_cognito_user(monkeypatch, _fake_delete)

        client.delete(ENDPOINT, headers=headers)

        assert received["access_token"] == "fake-access-token"
        assert received["sub"] == str(member.member_id)

    def test_deleted_at_recorded_after_cognito_success(self, client, db_session, monkeypatch):
        member = _create_member(db_session)
        member_id = member.member_id
        headers = _authenticate_as(monkeypatch, sub=str(member_id))
        _patch_delete_cognito_user(monkeypatch, lambda access_token, *, sub: None)

        # SQLite는 timezone-aware datetime을 저장 후 naive로 반환하므로
        # 비교 시 tz 정보를 벗겨서(UTC 기준) naive로 통일한다.
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        client.delete(ENDPOINT, headers=headers)
        after = datetime.now(timezone.utc).replace(tzinfo=None)

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.member_id == member_id).one()
        assert refreshed.deleted_at is not None
        recorded = refreshed.deleted_at
        if recorded.tzinfo is not None:
            recorded = recorded.astimezone(timezone.utc).replace(tzinfo=None)
        assert before <= recorded <= after


class TestWithdrawPendingMember:
    """
    이메일 인증을 끝내지 않은 PENDING 회원도 탈퇴할 수 있어야 한다.

    DELETE /users/me는 get_current_member(ACTIVE 검사 포함)가 아니라
    get_member_by_sub를 쓰므로 PENDING 회원도 이 endpoint에 도달한다.
    탈퇴 처리 분기는 status가 ACTIVE인지가 아니라 WITHDRAWN이 아닌지로
    판단하므로, PENDING -> WITHDRAWN 전이가 그대로 동작해야 한다.
    """

    def test_pending_member_withdrawal_returns_204(
        self, client, db_session, monkeypatch
    ):
        member = _create_member(db_session, status=MemberStatus.PENDING)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))
        _patch_delete_cognito_user(monkeypatch, lambda access_token, *, sub: None)

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 204

    def test_status_changes_pending_to_withdrawn(
        self, client, db_session, monkeypatch
    ):
        member = _create_member(db_session, status=MemberStatus.PENDING)
        member_id = member.member_id
        headers = _authenticate_as(monkeypatch, sub=str(member_id))
        _patch_delete_cognito_user(monkeypatch, lambda access_token, *, sub: None)

        client.delete(ENDPOINT, headers=headers)

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.member_id == member_id).one()
        assert refreshed.status == MemberStatus.WITHDRAWN
        assert refreshed.deleted_at is not None


class TestWithdrawAccessControl:
    def test_missing_authorization_returns_401(self, client, db_session):
        response = client.delete(ENDPOINT)
        assert response.status_code == 401

    def test_invalid_token_returns_401(self, client, db_session, monkeypatch):
        def _reject(_token):
            raise ValueError("Invalid Cognito token")

        monkeypatch.setattr(security, "verify_cognito_token", _reject)

        response = client.delete(
            ENDPOINT, headers={"Authorization": "Bearer forged-token"}
        )

        assert response.status_code == 401

    def test_member_not_found_returns_404(self, client, db_session, monkeypatch):
        headers = _authenticate_as(monkeypatch, sub=str(uuid.uuid4()))

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 404


class TestGetPatchBlockPendingMember:
    """
    이메일 인증 미완료(PENDING) 회원의 일반 API 접근 차단을 API 레벨에서
    검증한다. dependency 단위 테스트는 tests/test_current_member.py에
    있고, 여기서는 실제 HTTP 응답 형태(403 + code)를 확인한다.
    """

    def test_get_me_returns_403_for_pending_member(
        self, client, db_session, monkeypatch
    ):
        member = _create_member(db_session, status=MemberStatus.PENDING)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))

        response = client.get("/api/v1/users/me", headers=headers)

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "EMAIL_NOT_VERIFIED"

    def test_patch_me_returns_403_for_pending_member(
        self, client, db_session, monkeypatch
    ):
        member = _create_member(db_session, status=MemberStatus.PENDING)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))

        response = client.patch(
            "/api/v1/users/me", headers=headers, json={"nickname": "newnick"}
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "EMAIL_NOT_VERIFIED"


class TestGetPatchBlockWithdrawnMember:
    def test_get_me_returns_403_for_withdrawn_member(self, client, db_session, monkeypatch):
        member = _create_member(db_session, status=MemberStatus.WITHDRAWN)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))

        response = client.get("/api/v1/users/me", headers=headers)

        assert response.status_code == 403

    def test_patch_me_returns_403_for_withdrawn_member(self, client, db_session, monkeypatch):
        member = _create_member(db_session, status=MemberStatus.WITHDRAWN)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))

        response = client.patch(
            "/api/v1/users/me", headers=headers, json={"nickname": "new-nick"}
        )

        assert response.status_code == 403

    def test_get_me_returns_403_when_deleted_at_set_even_if_status_not_withdrawn(
        self, client, db_session, monkeypatch
    ):
        """방어적으로 deleted_at만 설정된 비정상 상태도 차단되어야 한다."""
        member = _create_member(
            db_session,
            status=MemberStatus.ACTIVE,
            deleted_at=datetime.now(timezone.utc),
        )
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))

        response = client.get("/api/v1/users/me", headers=headers)

        assert response.status_code == 403

    def test_get_me_still_200_for_active_member(self, client, db_session, monkeypatch):
        member = _create_member(db_session, status=MemberStatus.ACTIVE)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))

        response = client.get("/api/v1/users/me", headers=headers)

        assert response.status_code == 200


class TestWithdrawCognitoFailure:
    def test_cognito_delete_user_outage_returns_502_and_stays_withdrawn(
        self, client, db_session, monkeypatch
    ):
        member = _create_member(db_session)
        member_id = member.member_id
        headers = _authenticate_as(monkeypatch, sub=str(member_id))

        def _fake_delete_outage(access_token, *, sub):
            raise RuntimeError("Could not reach Cognito")

        _patch_delete_cognito_user(monkeypatch, _fake_delete_outage)

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 502

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.member_id == member_id).one()
        # DB는 이미 WITHDRAWN까지는 반영되어 재시도 가능해야 한다.
        assert refreshed.status == MemberStatus.WITHDRAWN
        assert refreshed.deleted_at is None

    def test_cognito_delete_user_token_rejected_returns_401_and_stays_withdrawn(
        self, client, db_session, monkeypatch
    ):
        member = _create_member(db_session)
        member_id = member.member_id
        headers = _authenticate_as(monkeypatch, sub=str(member_id))

        def _fake_delete_rejected(access_token, *, sub):
            raise ValueError("Cognito rejected the access token")

        _patch_delete_cognito_user(monkeypatch, _fake_delete_rejected)

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 401

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.member_id == member_id).one()
        assert refreshed.status == MemberStatus.WITHDRAWN
        assert refreshed.deleted_at is None


class TestWithdrawRetryAndIdempotency:
    def test_retry_when_withdrawn_and_deleted_at_null(self, client, db_session, monkeypatch):
        """케이스 A: status=WITHDRAWN, deleted_at=NULL. 이전 시도에서
        Cognito 삭제가 실패한 상태로 간주하고, DELETE 재시도를 허용한다."""
        member = _create_member(db_session, status=MemberStatus.WITHDRAWN, deleted_at=None)
        member_id = member.member_id
        headers = _authenticate_as(monkeypatch, sub=str(member_id))

        received = {}

        def _fake_delete(access_token, *, sub):
            received["called"] = True

        _patch_delete_cognito_user(monkeypatch, _fake_delete)

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 204
        assert received.get("called") is True

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.member_id == member_id).one()
        assert refreshed.status == MemberStatus.WITHDRAWN
        assert refreshed.deleted_at is not None

    def test_already_completed_withdrawal_returns_204_without_cognito_call(
        self, client, db_session, monkeypatch
    ):
        """케이스 B: status=WITHDRAWN, deleted_at!=NULL. 이미 탈퇴 완료된
        상태이므로 추가 DB 변경 없이 204를 반환하고 Cognito를 다시
        호출하지 않는다."""
        completed_at = datetime.now(timezone.utc)
        member = _create_member(
            db_session, status=MemberStatus.WITHDRAWN, deleted_at=completed_at
        )
        member_id = member.member_id
        headers = _authenticate_as(monkeypatch, sub=str(member_id))

        called = {"count": 0}

        def _fake_delete(access_token, *, sub):
            called["count"] += 1

        _patch_delete_cognito_user(monkeypatch, _fake_delete)

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 204
        assert called["count"] == 0

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.member_id == member_id).one()
        assert refreshed.deleted_at is not None


class TestWithdrawRequestBodyNotRequired:
    def test_no_body_needed_for_withdrawal(self, client, db_session, monkeypatch):
        """client가 member_id/sub/password를 request body로 보낼 필요가
        없어야 한다. body 없이 DELETE 호출로 정상 처리되어야 한다."""
        member = _create_member(db_session)
        headers = _authenticate_as(monkeypatch, sub=str(member.member_id))
        _patch_delete_cognito_user(monkeypatch, lambda access_token, *, sub: None)

        response = client.delete(ENDPOINT, headers=headers)

        assert response.status_code == 204
