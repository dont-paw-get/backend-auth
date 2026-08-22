"""
PATCH /api/v1/users/me 테스트.

실제 Cognito/AWS 없이 검증하기 위해 기존 test_users_me.py와 동일한
in-memory SQLite + dependency override 패턴을 재사용한다.
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


def _create_member(
    db_session,
    user_id="cognito-sub-0001",
    email=None,
    nickname=None,
    profile_image_url="https://cdn.example.com/old.png",
    agree_ai_analysis=False,
):
    now = datetime.now(timezone.utc)
    member = User(
        user_id=user_id,
        email=email or f"{user_id}@example.com",
        nickname=nickname or f"nick-{user_id}",
        profile_image_url=profile_image_url,
        agree_ai_analysis=agree_ai_analysis,
        agree_terms=True,
        agree_privacy=True,
        agreed_at=now,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _authenticate_as(user_id):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


class TestPatchUsersMeAllowedFields:
    def test_updates_nickname_only(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="old-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": "new-name"})

        assert response.status_code == 200
        body = response.json()
        assert body["nickname"] == "new-name"
        assert body["profile_image_url"] == "https://cdn.example.com/old.png"
        assert body["agree_ai_analysis"] is False

    def test_updates_profile_image_url_only(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="keep-name")
        _authenticate_as("sub-1")

        response = client.patch(
            ENDPOINT, json={"profile_image_url": "https://cdn.example.com/new.png"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["profile_image_url"] == "https://cdn.example.com/new.png"
        assert body["nickname"] == "keep-name"

    def test_removes_profile_image_url_with_explicit_null(self, client, db_session):
        _create_member(
            db_session,
            user_id="sub-1",
            profile_image_url="https://cdn.example.com/old.png",
        )
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"profile_image_url": None})

        assert response.status_code == 200
        assert response.json()["profile_image_url"] is None

    def test_updates_agree_ai_analysis_only(self, client, db_session):
        _create_member(db_session, user_id="sub-1", agree_ai_analysis=False)
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"agree_ai_analysis": True})

        assert response.status_code == 200
        assert response.json()["agree_ai_analysis"] is True

    def test_updates_multiple_fields_at_once(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="old-name", agree_ai_analysis=False)
        _authenticate_as("sub-1")

        response = client.patch(
            ENDPOINT,
            json={
                "nickname": "new-name",
                "profile_image_url": "https://cdn.example.com/new.png",
                "agree_ai_analysis": True,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["nickname"] == "new-name"
        assert body["profile_image_url"] == "https://cdn.example.com/new.png"
        assert body["agree_ai_analysis"] is True

    def test_fields_not_in_request_remain_unchanged(self, client, db_session):
        _create_member(
            db_session,
            user_id="sub-1",
            nickname="unchanged-name",
            profile_image_url="https://cdn.example.com/unchanged.png",
            agree_ai_analysis=True,
        )
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={})

        assert response.status_code == 200
        body = response.json()
        assert body["nickname"] == "unchanged-name"
        assert body["profile_image_url"] == "https://cdn.example.com/unchanged.png"
        assert body["agree_ai_analysis"] is True


class TestPatchUsersMeNicknameValidation:
    def test_nickname_null_is_rejected_without_db_error(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="old-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": None})

        # 422 validation error로 스키마 단계에서 거부되어야 하며,
        # DB commit까지 진행되어 IntegrityError/500이 발생하면 안 된다.
        assert response.status_code == 422

        # nickname이 실제로 변경되지 않았는지(=commit되지 않았는지) 확인.
        db_session.expire_all()
        stored = db_session.get(User, "sub-1")
        assert stored.nickname == "old-name"

    def test_nickname_empty_string_is_rejected(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="old-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": ""})

        assert response.status_code == 422

    def test_nickname_blank_string_is_rejected(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="old-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": "   "})

        assert response.status_code == 422

    def test_nickname_is_trimmed_before_saving(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="old-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": "  new-name  "})

        assert response.status_code == 200
        assert response.json()["nickname"] == "new-name"

    def test_nickname_conflict_check_uses_trimmed_value(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="my-name")
        _create_member(db_session, user_id="sub-2", nickname="used-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": "  used-name  "})

        assert response.status_code == 409


class TestPatchUsersMeNicknameConflict:
    def test_resubmitting_own_current_nickname_does_not_conflict(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="same-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": "same-name"})

        assert response.status_code == 200
        assert response.json()["nickname"] == "same-name"

    def test_nickname_already_used_by_another_member_returns_409(self, client, db_session):
        _create_member(db_session, user_id="sub-1", nickname="my-name")
        _create_member(db_session, user_id="sub-2", nickname="taken-name")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"nickname": "taken-name"})

        assert response.status_code == 409


class TestPatchUsersMeAuthAndNotFound:
    def test_returns_404_when_member_not_found(self, client):
        _authenticate_as("unknown-sub")

        response = client.patch(ENDPOINT, json={"nickname": "whatever"})

        assert response.status_code == 404

    def test_returns_501_when_auth_integration_not_overridden(self, client):
        response = client.patch(ENDPOINT, json={"nickname": "whatever"})

        assert response.status_code == 501


class TestPatchUsersMeDisallowedFields:
    def test_disallowed_field_in_body_is_rejected(self, client, db_session):
        _create_member(db_session, user_id="sub-1")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"email": "hacker@example.com"})

        # extra="forbid" 설정으로 FastAPI/Pydantic이 자동으로 422를 반환한다.
        assert response.status_code == 422

    def test_user_id_in_body_is_rejected(self, client, db_session):
        _create_member(db_session, user_id="sub-1")
        _authenticate_as("sub-1")

        response = client.patch(ENDPOINT, json={"user_id": "someone-else"})

        assert response.status_code == 422
