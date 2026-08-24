"""
CLIAR-87 신규 모델(terms, member_agreement, member_librarian)에 대한
최소 metadata 검증 테스트.

이 테이블들은 아직 API/service가 없으므로(이번 CLIAR-87 범위 밖),
SQLAlchemy 모델의 컬럼 제약조건과 FK/ENUM 정의만 검증한다.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.member_agreement import MemberAgreement, MemberAgreementAction
from app.models.member_librarian import MemberLibrarian
from app.models.terms import Terms
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


class TestTermsTableDefinition:
    def test_table_name_is_terms(self):
        assert Terms.__tablename__ == "terms"

    def test_id_is_primary_key(self):
        assert Terms.__table__.c.id.primary_key is True

    def test_code_name_content_are_not_null(self):
        assert Terms.__table__.c.code.nullable is False
        assert Terms.__table__.c.name.nullable is False
        assert Terms.__table__.c.content.nullable is False

    def test_is_required_defaults_to_false(self):
        column = Terms.__table__.c.is_required
        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg is False

    def test_effective_at_is_not_null(self):
        assert Terms.__table__.c.effective_at.nullable is False

    def test_expired_at_is_nullable(self):
        assert Terms.__table__.c.expired_at.nullable is True

    def test_no_version_column(self):
        """정책: 별도의 version 컬럼은 사용하지 않는다."""
        columns = {c.name for c in Terms.__table__.columns}
        assert "version" not in columns


class TestMemberAgreementTableDefinition:
    def test_table_name_is_member_agreement(self):
        assert MemberAgreement.__tablename__ == "member_agreement"

    def test_member_id_has_fk_to_member(self):
        column = MemberAgreement.__table__.c.member_id
        assert column.nullable is False
        fk_targets = {fk.target_fullname for fk in column.foreign_keys}
        assert "member.member_id" in fk_targets

    def test_terms_id_has_fk_to_terms(self):
        column = MemberAgreement.__table__.c.terms_id
        assert column.nullable is False
        fk_targets = {fk.target_fullname for fk in column.foreign_keys}
        assert "terms.id" in fk_targets

    def test_action_is_not_null(self):
        assert MemberAgreement.__table__.c.action.nullable is False

    def test_action_enum_has_agree_and_withdraw(self):
        assert set(MemberAgreementAction) == {
            MemberAgreementAction.AGREE,
            MemberAgreementAction.WITHDRAW,
        }

    def test_occurred_at_is_not_null(self):
        assert MemberAgreement.__table__.c.occurred_at.nullable is False

    def test_deleted_at_is_nullable(self):
        assert MemberAgreement.__table__.c.deleted_at.nullable is True


class TestMemberLibrarianTableDefinition:
    def test_table_name_is_member_librarian(self):
        assert MemberLibrarian.__tablename__ == "member_librarian"

    def test_member_id_has_fk_to_member(self):
        column = MemberLibrarian.__table__.c.member_id
        assert column.nullable is False
        fk_targets = {fk.target_fullname for fk in column.foreign_keys}
        assert "member.member_id" in fk_targets

    def test_librarian_id_has_no_fk(self):
        """librarian_id는 Librarian 서비스가 소유하므로 FK를 걸지 않는다."""
        column = MemberLibrarian.__table__.c.librarian_id
        assert column.nullable is False
        assert len(column.foreign_keys) == 0

    def test_evolution_stage_defaults_to_one(self):
        column = MemberLibrarian.__table__.c.evolution_stage
        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg == 1

    def test_is_representative_defaults_to_false(self):
        column = MemberLibrarian.__table__.c.is_representative
        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg is False

    def test_no_unique_constraint_on_member_id_and_librarian_id(self):
        """정책: 같은 librarian_id를 여러 인스턴스로 보유할 수 있으므로
        (member_id, librarian_id) UNIQUE는 추가하지 않는다."""
        unique_constraints = [
            tuple(sorted(c.columns.keys()))
            for c in MemberLibrarian.__table__.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        ]
        assert ("librarian_id", "member_id") not in unique_constraints


class TestCrossModelPersistence:
    def test_member_agreement_and_librarian_can_reference_member(self, db_session):
        """실제로 member row를 만들고, member_agreement/member_librarian이
        그 member_id를 참조하는 row를 생성할 수 있는지 최소 확인한다."""
        member_id = uuid.uuid4()
        member = User(
            member_id=member_id,
            email="member@example.com",
            nickname="membernick",
            status=MemberStatus.ACTIVE,
        )
        db_session.add(member)
        db_session.commit()

        terms = Terms(
            code="TERMS_OF_SERVICE",
            name="이용약관",
            content="약관 내용",
            effective_at=member.created_at,
        )
        db_session.add(terms)
        db_session.commit()

        agreement = MemberAgreement(
            member_id=member_id,
            terms_id=terms.id,
            action=MemberAgreementAction.AGREE,
        )
        librarian = MemberLibrarian(
            member_id=member_id,
            librarian_id=1,
        )
        db_session.add_all([agreement, librarian])
        db_session.commit()

        assert agreement.id is not None
        assert librarian.id is not None
