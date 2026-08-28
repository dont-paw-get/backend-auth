"""
User 모델(테이블명 member) 테스트.

실제 PostgreSQL(AWS RDS 등)에는 연결하지 않는다.
대신 in-memory SQLite 엔진에 모델 메타데이터로 테이블을 생성해
컬럼 제약조건(NOT NULL / UNIQUE / PK / default)이 정의대로 동작하는지 검증한다.

CLIAR-87: member_id(UUID)가 새 UNIQUE 식별자이며, 내부 PK는 id(BIGINT)이다.
status는 MemberStatus ENUM(PENDING/ACTIVE/WITHDRAWN)이고, nickname UNIQUE는 제거되었으며,
약관 관련 컬럼(agree_terms 등)은 더 이상 이 테이블에 존재하지 않는다.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.user import MemberStatus, User


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
    defaults = dict(
        member_id=uuid.uuid4(),
        email="user@example.com",
        nickname="nickname01",
        status=MemberStatus.ACTIVE,
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

    def test_agreement_columns_are_not_present(self):
        """약관 동의 관련 컬럼은 terms/member_agreement로 이관되어 제거되었다."""
        columns = {c.name for c in User.__table__.columns}
        assert "agree_terms" not in columns
        assert "agree_privacy" not in columns
        assert "agreed_at" not in columns
        assert "agree_ai_analysis" not in columns
        assert "ai_analysis_consent_updated_at" not in columns
        assert "representative_librarian_id" not in columns

    def test_id_is_primary_key(self):
        column = User.__table__.c.id
        assert column.primary_key is True
        assert column.nullable is False

    def test_member_id_is_not_null_and_unique(self):
        column = User.__table__.c.member_id
        assert column.nullable is False
        assert column.unique is True

    def test_email_is_not_null_and_unique(self):
        column = User.__table__.c.email
        assert column.nullable is False
        assert column.unique is True

    def test_nickname_is_not_null_and_not_unique(self):
        """CLIAR-87: nickname UNIQUE 제약은 제거되어 중복을 허용한다."""
        column = User.__table__.c.nickname
        assert column.nullable is False
        assert not column.unique

    def test_profile_image_url_is_nullable(self):
        column = User.__table__.c.profile_image_url
        assert column.nullable is True

    def test_status_is_not_null_enum(self):
        column = User.__table__.c.status
        assert column.nullable is False

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
        member_id = uuid.uuid4()
        user = _make_user(member_id=member_id)
        db_session.add(user)
        db_session.commit()

        fetched = (
            db_session.query(User).filter(User.member_id == member_id).one_or_none()
        )
        assert fetched is not None
        assert fetched.email == "user@example.com"
        assert fetched.nickname == "nickname01"
        assert fetched.status == MemberStatus.ACTIVE
        assert fetched.profile_image_url is None

    def test_email_uniqueness_is_enforced(self, db_session):
        db_session.add(_make_user(nickname="nick-1"))
        db_session.commit()

        db_session.add(_make_user(email="user@example.com", nickname="nick-2"))
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_nickname_can_be_duplicated(self, db_session):
        """CLIAR-87: nickname 중복은 더 이상 DB 제약 위반이 아니다."""
        db_session.add(_make_user(email="a@example.com", nickname="dup"))
        db_session.commit()

        db_session.add(_make_user(email="b@example.com", nickname="dup"))
        db_session.commit()  # 예외가 발생하지 않아야 한다.

        count = db_session.query(User).filter(User.nickname == "dup").count()
        assert count == 2

    def test_member_id_uniqueness_is_enforced(self, db_session):
        shared_id = uuid.uuid4()
        db_session.add(_make_user(member_id=shared_id, email="a@example.com", nickname="nick-a"))
        db_session.commit()

        db_session.add(_make_user(member_id=shared_id, email="b@example.com", nickname="nick-b"))
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_missing_required_status_raises(self, db_session):
        user = _make_user(status=None)
        db_session.add(user)
        with pytest.raises(IntegrityError):
            db_session.commit()
