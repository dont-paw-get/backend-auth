"""
POST /api/v1/users/bootstrap 테스트

최초 MEMBER 생성 API 검증.
실제 Cognito/AWS 없이 request body의 trusted identity를 전달하는 방식으로 테스트한다.
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


ENDPOINT = "/api/v1/users/bootstrap"


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
    user_id="existing-sub",
    email="existing@example.com",
    nickname="existing-nick",
):
    member = User(
        user_id=user_id,
        email=email,
        nickname=nickname,
        agree_terms=True,
        agree_privacy=True,
        agreed_at=datetime.now(timezone.utc),
    )

    db_session.add(member)
    db_session.commit()

    return member


class TestBootstrapMember:


    def test_creates_member_successfully(self, client, db_session):
        response = client.post(
            ENDPOINT,
            json={
                "user_id": "cognito-sub-001",
                "email": "test@example.com",
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_ai_analysis": False,
            },
        )

        assert response.status_code == 201

        body = response.json()

        assert body["user_id"] == "cognito-sub-001"
        assert body["email"] == "test@example.com"
        assert body["nickname"] == "haechan"


    def test_member_is_saved_in_database(self, client, db_session):
        client.post(
            ENDPOINT,
            json={
                "user_id": "cognito-sub-001",
                "email": "test@example.com",
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        member = db_session.query(User).first()

        assert member is not None
        assert member.user_id == "cognito-sub-001"


class TestBootstrapValidation:


    def test_duplicate_user_id_returns_409(self, client, db_session):
        _create_member(
            db_session,
            user_id="same-sub",
            email="old@example.com",
            nickname="oldnick",
        )

        response = client.post(
            ENDPOINT,
            json={
                "user_id": "same-sub",
                "email": "new@example.com",
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 409


    def test_duplicate_email_returns_409(self, client, db_session):
        _create_member(
            db_session,
            user_id="old-sub",
            email="same@example.com",
            nickname="oldnick",
        )

        response = client.post(
            ENDPOINT,
            json={
                "user_id": "new-sub",
                "email": "same@example.com",
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 409


    def test_duplicate_nickname_returns_409(self, client, db_session):
        _create_member(
            db_session,
            user_id="old-sub",
            email="old@example.com",
            nickname="same-nick",
        )

        response = client.post(
            ENDPOINT,
            json={
                "user_id": "new-sub",
                "email": "new@example.com",
                "nickname": "same-nick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 409


    def test_required_consent_false_returns_400(self, client):

        response = client.post(
            ENDPOINT,
            json={
                "user_id": "new-sub",
                "email": "new@example.com",
                "nickname": "newnick",
                "agree_terms": False,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 400


    def test_blank_nickname_returns_400(self, client):

        response = client.post(
            ENDPOINT,
            json={
                "user_id": "new-sub",
                "email": "new@example.com",
                "nickname": "   ",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 400