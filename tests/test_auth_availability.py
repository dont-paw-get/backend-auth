"""
POST /api/v1/auth/availability 테스트.

실제 PostgreSQL(AWS RDS 등)에는 연결하지 않는다.
FastAPI의 get_db 의존성을 in-memory SQLite 세션으로 override 하여
API -> Service -> Repository -> DB 흐름을 검증한다.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models.user import User

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


def _create_user(db_session, *, email="taken@example.com", nickname="takennick"):
    now = datetime.now(timezone.utc)
    user = User(
        user_id="existing-user",
        email=email,
        nickname=nickname,
        agree_terms=True,
        agree_privacy=True,
        agreed_at=now,
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
