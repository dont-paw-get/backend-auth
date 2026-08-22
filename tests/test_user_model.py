"""
User 모델 테스트.

실제 PostgreSQL(AWS RDS 등)에는 연결하지 않는다.
대신 in-memory SQLite 엔진에 모델 메타데이터로 테이블을 생성해
컬럼 제약조건(NOT NULL / UNIQUE / PK / default)이 정의대로 동작하는지 검증한다.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.user import User


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(engine):
    session = Session(bind=engine)
    yield session
    session.close()


def _make_user(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        user_id="cognito-sub-0001",
        email="user@example.com",
        nickname="nickname01",
        agree_terms=True,
        agree_privacy=True,
        agreed_at=now,
    )
    defaults.update(overrides)
    return User(**defaults)


class TestUserTableDefinition:
    def test_table_name_is_member(self):
        assert User.__tablename__ == "member"

    def test_password_fields_are_not_present(self):
        columns = {c.name for c in User.__table__.columns}
        assert "password" not in columns
        assert "password_hash" not in columns

    def test_user_id_is_primary_key(self):
        column = User.__table__.c.user_id
        assert column.primary_key is True
        assert column.nullable is False

    def test_email_is_not_null_and_unique(self):
        column = User.__table__.c.email
        assert column.nullable is False
        assert column.unique is True

    def test_nickname_is_not_null_and_unique(self):
        column = User.__table__.c.nickname
        assert column.nullable is False
        assert column.unique is True

    def test_profile_image_url_is_nullable(self):
        column = User.__table__.c.profile_image_url
        assert column.nullable is True

    def test_representative_librarian_id_is_nullable_without_fk(self):
        column = User.__table__.c.representative_librarian_id
        assert column.nullable is True
        assert len(column.foreign_keys) == 0

    def test_status_is_not_null_with_pending_default(self):
        column = User.__table__.c.status
        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg == "PENDING"

    def test_agree_terms_is_not_null_boolean(self):
        column = User.__table__.c.agree_terms
        assert column.nullable is False

    def test_agree_privacy_is_not_null_boolean(self):
        column = User.__table__.c.agree_privacy
        assert column.nullable is False

    def test_agreed_at_is_not_null(self):
        column = User.__table__.c.agreed_at
        assert column.nullable is False

    def test_agree_ai_analysis_is_not_null_with_false_default(self):
        column = User.__table__.c.agree_ai_analysis
        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg is False

    def test_ai_analysis_consent_updated_at_is_nullable(self):
        column = User.__table__.c.ai_analysis_consent_updated_at
        assert column.nullable is True

    def test_created_at_is_not_null(self):
        column = User.__table__.c.created_at
        assert column.nullable is False

    def test_updated_at_is_not_null(self):
        column = User.__table__.c.updated_at
        assert column.nullable is False

    def test_deleted_at_is_nullable(self):
        column = User.__table__.c.deleted_at
        assert column.nullable is True


class TestUserPersistence:
    def test_insert_minimal_valid_user(self, db_session):
        user = _make_user()
        db_session.add(user)
        db_session.commit()

        fetched = db_session.get(User, "cognito-sub-0001")
        assert fetched is not None
        assert fetched.email == "user@example.com"
        assert fetched.nickname == "nickname01"
        assert fetched.status == "PENDING"
        assert fetched.agree_ai_analysis is False
        assert fetched.profile_image_url is None
        assert fetched.representative_librarian_id is None

    def test_email_uniqueness_is_enforced(self, db_session):
        db_session.add(_make_user(user_id="user-1", nickname="nick-1"))
        db_session.commit()

        db_session.add(
            _make_user(user_id="user-2", email="user@example.com", nickname="nick-2")
        )
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_nickname_uniqueness_is_enforced(self, db_session):
        db_session.add(_make_user(user_id="user-1", email="a@example.com", nickname="dup"))
        db_session.commit()

        db_session.add(_make_user(user_id="user-2", email="b@example.com", nickname="dup"))
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_missing_required_agree_terms_raises(self, db_session):
        user = _make_user(agree_terms=None)
        db_session.add(user)
        with pytest.raises(IntegrityError):
            db_session.commit()
