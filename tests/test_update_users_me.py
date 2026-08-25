"""
PATCH /api/v1/users/me 테스트.

실제 Cognito/AWS 없이 검증하기 위해 기존 test_users_me.py와 동일한
in-memory SQLite + dependency override 패턴을 재사용한다.

CLIAR-87: agree_ai_analysis는 member 컬럼에서 제거되어 더 이상 이 API로
수정할 수 없다(MemberUpdateRequest에서 필드 자체가 제거됨). 인증
sub/member_id는 UUID 문자열이다.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import get_current_user_id
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
    member_id,
    email=None,
    nickname=None,
    profile_image_url="https://cdn.example.com/old.png",
):
    member = User(
        member_id=member_id,
        email=email or f"{member_id}@example.com",
        nickname=nickname or f"nick-{member_id.hex[:8]}",
        profile_image_url=profile_image_url,
        status=MemberStatus.ACTIVE,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _authenticate_as(member_id):
    app.dependency_overrides[get_current_user_id] = lambda: str(member_id)


class TestPatchUsersMeAllowedFields:
    def test_updates_nickname_only(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="old-name")
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"nickname": "new-name"})

        assert response.status_code == 200
        body = response.json()
        assert body["nickname"] == "new-name"
        assert body["profile_image_url"] == "https://cdn.example.com/old.png"

    def test_updates_profile_image_url_only(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="keep-name")
        _authenticate_as(member_id)

        response = client.patch(
            ENDPOINT, json={"profile_image_url": "https://cdn.example.com/new.png"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["profile_image_url"] == "https://cdn.example.com/new.png"
        assert body["nickname"] == "keep-name"

    def test_removes_profile_image_url_with_explicit_null(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(
            db_session,
            member_id,
            profile_image_url="https://cdn.example.com/old.png",
        )
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"profile_image_url": None})

        assert response.status_code == 200
        assert response.json()["profile_image_url"] is None

    def test_updates_multiple_fields_at_once(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="old-name")
        _authenticate_as(member_id)

        response = client.patch(
            ENDPOINT,
            json={
                "nickname": "new-name",
                "profile_image_url": "https://cdn.example.com/new.png",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["nickname"] == "new-name"
        assert body["profile_image_url"] == "https://cdn.example.com/new.png"

    def test_fields_not_in_request_remain_unchanged(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(
            db_session,
            member_id,
            nickname="unchanged-name",
            profile_image_url="https://cdn.example.com/unchanged.png",
        )
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={})

        assert response.status_code == 200
        body = response.json()
        assert body["nickname"] == "unchanged-name"
        assert body["profile_image_url"] == "https://cdn.example.com/unchanged.png"

    def test_agree_ai_analysis_field_is_no_longer_accepted(self, client, db_session):
        """CLIAR-87: agree_ai_analysis는 member 컬럼에서 제거되어 이 스키마
        (extra="forbid")가 더 이상 허용하지 않는다."""
        member_id = uuid.uuid4()
        _create_member(db_session, member_id)
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"agree_ai_analysis": True})

        assert response.status_code == 422


class TestPatchUsersMeNicknameValidation:
    def test_nickname_null_is_rejected_without_db_error(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="old-name")
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"nickname": None})

        # 422 validation error로 스키마 단계에서 거부되어야 하며,
        # DB commit까지 진행되어 IntegrityError/500이 발생하면 안 된다.
        assert response.status_code == 422

        # nickname이 실제로 변경되지 않았는지(=commit되지 않았는지) 확인.
        db_session.expire_all()
        stored = db_session.query(User).filter(User.member_id == member_id).one()
        assert stored.nickname == "old-name"

    def test_nickname_empty_string_is_rejected(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="old-name")
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"nickname": ""})

        assert response.status_code == 422

    def test_nickname_blank_string_is_rejected(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="old-name")
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"nickname": "   "})

        assert response.status_code == 422

    def test_nickname_is_trimmed_before_saving(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="old-name")
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"nickname": "  new-name  "})

        assert response.status_code == 200
        assert response.json()["nickname"] == "new-name"

    def test_nickname_matching_another_member_after_trim_is_allowed(self, client, db_session):
        """CLIAR-87: nickname 중복은 허용되므로, trim된 값이 다른 회원의
        nickname과 같아도 409가 아니라 200이어야 한다."""
        member_id_a = uuid.uuid4()
        member_id_b = uuid.uuid4()
        _create_member(db_session, member_id_a, nickname="my-name")
        _create_member(db_session, member_id_b, nickname="used-name")
        _authenticate_as(member_id_a)

        response = client.patch(ENDPOINT, json={"nickname": "  used-name  "})

        assert response.status_code == 200
        assert response.json()["nickname"] == "used-name"


class TestPatchUsersMeNicknameConflict:
    def test_resubmitting_own_current_nickname_does_not_conflict(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id, nickname="same-name")
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"nickname": "same-name"})

        assert response.status_code == 200
        assert response.json()["nickname"] == "same-name"

    def test_nickname_already_used_by_another_member_is_allowed(self, client, db_session):
        """CLIAR-87 확정 요구사항: member.nickname은 UNIQUE 제약이 없으며
        중복을 허용한다. 다른 회원이 이미 사용 중인 nickname으로
        변경해도 409가 아니라 200이어야 한다."""
        member_id_a = uuid.uuid4()
        member_id_b = uuid.uuid4()
        _create_member(db_session, member_id_a, nickname="my-name")
        _create_member(db_session, member_id_b, nickname="taken-name")
        _authenticate_as(member_id_a)

        response = client.patch(ENDPOINT, json={"nickname": "taken-name"})

        assert response.status_code == 200
        assert response.json()["nickname"] == "taken-name"


class TestPatchUsersMeAuthAndNotFound:
    def test_returns_404_when_member_not_found(self, client):
        _authenticate_as(uuid.uuid4())

        response = client.patch(ENDPOINT, json={"nickname": "whatever"})

        assert response.status_code == 404

    def test_returns_401_when_auth_integration_not_overridden(self, client):
        """CLIAR-105: Cognito 인증 연동이 실제로 구현되었으므로 더 이상
        501(CLIAR-71 시절의 임시 정책)이 아니라 401을 반환해야 한다."""
        response = client.patch(ENDPOINT, json={"nickname": "whatever"})

        assert response.status_code == 401


class TestPatchUsersMeDisallowedFields:
    def test_disallowed_field_in_body_is_rejected(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id)
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"email": "hacker@example.com"})

        # extra="forbid" 설정으로 FastAPI/Pydantic이 자동으로 422를 반환한다.
        assert response.status_code == 422

    def test_member_id_in_body_is_rejected(self, client, db_session):
        member_id = uuid.uuid4()
        _create_member(db_session, member_id)
        _authenticate_as(member_id)

        response = client.patch(ENDPOINT, json={"member_id": str(uuid.uuid4())})

        assert response.status_code == 422
