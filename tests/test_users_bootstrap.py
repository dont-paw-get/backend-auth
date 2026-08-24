"""
POST /api/v1/users/bootstrap 테스트

최초 MEMBER 생성 API 검증.
실제 Cognito/AWS 없이 request body의 trusted identity를 전달하는 방식으로 테스트한다.

CLIAR-87: user_id 요청 필드와 응답의 member_id는 UUID 문자열이다.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models.user import MemberStatus, User


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
    member_id=None,
    email="existing@example.com",
    nickname="existing-nick",
):
    member = User(
        member_id=member_id or uuid.uuid4(),
        email=email,
        nickname=nickname,
        status=MemberStatus.ACTIVE,
    )

    db_session.add(member)
    db_session.commit()

    return member


class TestBootstrapMember:

    def test_creates_member_successfully(self, client, db_session):
        member_id = str(uuid.uuid4())
        response = client.post(
            ENDPOINT,
            json={
                "user_id": member_id,
                "email": "test@example.com",
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_ai_analysis": False,
            },
        )

        assert response.status_code == 201

        body = response.json()

        assert body["member_id"] == member_id
        assert body["email"] == "test@example.com"
        assert body["nickname"] == "haechan"

    def test_member_is_saved_in_database(self, client, db_session):
        member_id = str(uuid.uuid4())
        client.post(
            ENDPOINT,
            json={
                "user_id": member_id,
                "email": "test@example.com",
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        member = db_session.query(User).first()

        assert member is not None
        assert str(member.member_id) == member_id


class TestBootstrapValidation:

    def test_duplicate_user_id_returns_409(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(
            db_session,
            member_id=member_id,
            email="old@example.com",
            nickname="oldnick",
        )

        response = client.post(
            ENDPOINT,
            json={
                "user_id": str(member_id),
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
            email="same@example.com",
            nickname="oldnick",
        )

        response = client.post(
            ENDPOINT,
            json={
                "user_id": str(uuid.uuid4()),
                "email": "same@example.com",
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 409

    def test_duplicate_nickname_is_allowed(self, client, db_session):
        """CLIAR-87 확정 요구사항: member.nickname은 UNIQUE 제약이 없으며
        중복을 허용한다. 다른 회원과 동일한 nickname으로도 정상 생성되어야
        한다."""
        _create_member(
            db_session,
            email="old@example.com",
            nickname="same-nick",
        )

        response = client.post(
            ENDPOINT,
            json={
                "user_id": str(uuid.uuid4()),
                "email": "new@example.com",
                "nickname": "same-nick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 201
        assert response.json()["nickname"] == "same-nick"

    def test_required_consent_false_returns_400(self, client):

        response = client.post(
            ENDPOINT,
            json={
                "user_id": str(uuid.uuid4()),
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
                "user_id": str(uuid.uuid4()),
                "email": "new@example.com",
                "nickname": "   ",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 400

    def test_non_uuid_user_id_returns_422(self, client):
        """CLIAR-87: user_id는 UUID 형식이어야 하며, 그렇지 않으면 Pydantic
        검증 단계에서 422로 거부된다."""
        response = client.post(
            ENDPOINT,
            json={
                "user_id": "not-a-uuid",
                "email": "new@example.com",
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 422
