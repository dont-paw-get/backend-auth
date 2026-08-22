"""
GET /api/v1/users/me 테스트.

실제 Cognito/AWS 없이 검증하기 위해:
- 인증 dependency(get_current_user_id)는 app.dependency_overrides로 override
- DB는 get_db를 override하여 in-memory SQLite 세션을 주입
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import get_current_user_id
from app.main import app
from app.models.user import User

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


def _create_member(db_session, user_id="cognito-sub-0001", email=None, nickname=None):
    now = datetime.now(timezone.utc)
    member = User(
        user_id=user_id,
        email=email or f"{user_id}@example.com",
        nickname=nickname or f"nick-{user_id}",
        profile_image_url="https://cdn.example.com/profile.png",
        agree_terms=True,
        agree_privacy=True,
        agreed_at=now,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


class TestGetUsersMe:
    def test_returns_200_with_member_info_when_authenticated(self, client, db_session):
        _create_member(db_session, user_id="cognito-sub-0001")
        app.dependency_overrides[get_current_user_id] = lambda: "cognito-sub-0001"

        response = client.get(ENDPOINT)

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "cognito-sub-0001"
        assert body["email"] == "cognito-sub-0001@example.com"
        assert body["nickname"] == "nick-cognito-sub-0001"
        assert body["profile_image_url"] == "https://cdn.example.com/profile.png"
        assert body["status"] == "PENDING"
        assert body["agree_ai_analysis"] is False
        assert "created_at" in body
        assert "updated_at" in body

    def test_response_does_not_contain_password_fields(self, client, db_session):
        _create_member(db_session, user_id="cognito-sub-0001")
        app.dependency_overrides[get_current_user_id] = lambda: "cognito-sub-0001"

        response = client.get(ENDPOINT)

        body = response.json()
        assert "password" not in body
        assert "password_hash" not in body

    def test_response_does_not_contain_agreement_fields_outside_scope(self, client, db_session):
        """
        agree_terms / agree_privacy / agreed_at은 이번 API 응답 범위에
        포함되지 않는다.
        """
        _create_member(db_session, user_id="cognito-sub-0001")
        app.dependency_overrides[get_current_user_id] = lambda: "cognito-sub-0001"

        response = client.get(ENDPOINT)

        body = response.json()
        assert "agree_terms" not in body
        assert "agree_privacy" not in body
        assert "agreed_at" not in body

    def test_returns_member_matching_overridden_sub(self, client, db_session):
        _create_member(db_session, user_id="cognito-sub-AAA")
        _create_member(db_session, user_id="cognito-sub-BBB")
        app.dependency_overrides[get_current_user_id] = lambda: "cognito-sub-BBB"

        response = client.get(ENDPOINT)

        assert response.status_code == 200
        assert response.json()["user_id"] == "cognito-sub-BBB"

    def test_returns_404_when_member_not_found(self, client):
        app.dependency_overrides[get_current_user_id] = lambda: "unknown-sub"

        response = client.get(ENDPOINT)

        assert response.status_code == 404

    def test_returns_501_when_auth_integration_not_overridden(self, client):
        """
        get_current_user_id를 override하지 않으면, 기존 501 placeholder
        동작이 그대로 유지되어야 한다.
        """
        response = client.get(ENDPOINT)

        assert response.status_code == 501
