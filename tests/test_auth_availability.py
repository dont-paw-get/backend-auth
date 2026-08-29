"""
POST /api/v1/auth/availability 테스트.

실제 PostgreSQL(AWS RDS 등)에는 연결하지 않는다.
FastAPI의 get_db 의존성을 in-memory SQLite 세션으로 override 하여
API -> Service -> Repository -> DB 흐름을 검증한다.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models.user import MemberStatus, User

ENDPOINT = "/api/v1/auth/availability"


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


def _create_user(
    db_session,
    *,
    email="taken@example.com",
    nickname="takennick",
    status=MemberStatus.ACTIVE,
    deleted_at=None,
):
    user = User(
        member_id=uuid.uuid4(),
        email=email,
        nickname=nickname,
        status=status,
        deleted_at=deleted_at,
    )
    db_session.add(user)
    db_session.commit()
    return user


class TestEmailAvailability:
    def test_email_not_taken_returns_available_true(self, client):
        response = client.post(ENDPOINT, json={"field": "EMAIL", "value": "new@example.com"})

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": True}

    def test_email_taken_returns_available_false(self, client, db_session):
        _create_user(db_session, email="taken@example.com")

        response = client.post(ENDPOINT, json={"field": "EMAIL", "value": "taken@example.com"})

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": False}

    def test_email_uppercase_is_normalized_to_lowercase(self, client, db_session):
        _create_user(db_session, email="taken@example.com")

        response = client.post(ENDPOINT, json={"field": "EMAIL", "value": "Taken@Example.com"})

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": False}

    def test_email_surrounding_whitespace_is_trimmed(self, client, db_session):
        _create_user(db_session, email="taken@example.com")

        response = client.post(
            ENDPOINT, json={"field": "EMAIL", "value": "  taken@example.com  "}
        )

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": False}

    def test_pending_email_returns_available_false(self, client, db_session):
        _create_user(
            db_session, email="pending@example.com", status=MemberStatus.PENDING
        )

        response = client.post(ENDPOINT, json={"field": "EMAIL", "value": "pending@example.com"})

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": False}

    def test_withdrawal_in_progress_email_returns_available_false(self, client, db_session):
        """CLIAR-177: status=WITHDRAWN이지만 deleted_at이 아직 없는
        경우(탈퇴 처리 중)는 사용 가능으로 보고하면 안 된다."""
        _create_user(
            db_session,
            email="mid-withdrawal@example.com",
            status=MemberStatus.WITHDRAWN,
            deleted_at=None,
        )

        response = client.post(
            ENDPOINT, json={"field": "EMAIL", "value": "mid-withdrawal@example.com"}
        )

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": False}

    def test_completed_withdrawal_email_returns_available_true(self, client, db_session):
        """CLIAR-177: 탈퇴 완료(deleted_at 설정됨)된 회원의 이메일은
        재가입 정책과 동일하게 사용 가능(available=True)으로
        보고해야 한다."""
        _create_user(
            db_session,
            email="withdrawn@example.com",
            status=MemberStatus.WITHDRAWN,
            deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

        response = client.post(ENDPOINT, json={"field": "EMAIL", "value": "withdrawn@example.com"})

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": True}

    def test_email_reused_by_a_new_signup_returns_available_false_again(
        self, client, db_session
    ):
        """CLIAR-177: 탈퇴 완료 회원의 이메일로 재가입이 실제로
        일어나면(과거 WITHDRAWN row + 새 PENDING row가 같은 이메일로
        공존), 다시 available=false여야 한다 — 과거 row에 흔들리지
        않고 새로 생긴 현재 row를 정확히 반영해야 한다."""
        _create_user(
            db_session,
            email="recycled@example.com",
            status=MemberStatus.WITHDRAWN,
            deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        _create_user(
            db_session,
            email="recycled@example.com",
            nickname="recycled-new",
            status=MemberStatus.PENDING,
        )

        response = client.post(ENDPOINT, json={"field": "EMAIL", "value": "recycled@example.com"})

        assert response.status_code == 200
        assert response.json() == {"field": "EMAIL", "available": False}


class TestNicknameAvailability:
    def test_nickname_not_taken_returns_available_true(self, client):
        response = client.post(ENDPOINT, json={"field": "NICKNAME", "value": "freshnick"})

        assert response.status_code == 200
        assert response.json() == {"field": "NICKNAME", "available": True}

    def test_nickname_taken_returns_available_false(self, client, db_session):
        _create_user(db_session, nickname="takennick")

        response = client.post(ENDPOINT, json={"field": "NICKNAME", "value": "takennick"})

        assert response.status_code == 200
        assert response.json() == {"field": "NICKNAME", "available": False}

    def test_nickname_surrounding_whitespace_is_trimmed(self, client, db_session):
        _create_user(db_session, nickname="takennick")

        response = client.post(ENDPOINT, json={"field": "NICKNAME", "value": "  takennick  "})

        assert response.status_code == 200
        assert response.json() == {"field": "NICKNAME", "available": False}


class TestInvalidRequest:
    def test_unsupported_field_returns_400(self, client):
        response = client.post(ENDPOINT, json={"field": "USERNAME", "value": "something"})

        assert response.status_code == 400

    def test_empty_value_returns_400(self, client):
        response = client.post(ENDPOINT, json={"field": "EMAIL", "value": ""})

        assert response.status_code == 400

    def test_blank_value_returns_400(self, client):
        response = client.post(ENDPOINT, json={"field": "NICKNAME", "value": "   "})

        assert response.status_code == 400

    def test_missing_field_key_returns_400_not_422(self, client):
        response = client.post(ENDPOINT, json={"value": "something"})

        assert response.status_code == 400

    def test_missing_value_key_returns_400_not_422(self, client):
        response = client.post(ENDPOINT, json={"field": "EMAIL"})

        assert response.status_code == 400
