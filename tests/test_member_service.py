"""
MEMBER 최초 생성 service(app/services/member_service.py) 테스트.

이번 Jira는 FastAPI endpoint가 없는 순수 service/repository 계층이므로,
TestClient 없이 in-memory SQLite 세션 + UserRepository를 직접 사용해
검증한다. TrustedIdentity(user_id/email)는 이미 인증 계층에서 신뢰된
값이라고 가정하며, 이 테스트에서도 HTTP 헤더나 Cognito 호출을 흉내내지
않고 값을 직접 구성해서 넘긴다.

CLIAR-87: TrustedIdentity.user_id는 UUID이고, 생성된 member는
member_id(UUID)를 가진다. agree_terms/agree_privacy 필수 동의 검증은
유지되지만, member 테이블에는 더 이상 이 값들이 저장되지 않는다
(terms/member_agreement 이관은 이번 CLIAR-87 API 범위 밖).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.user import MemberStatus, User
from app.repositories.user_repository import UserRepository
from app.services.member_service import (
    EmailAlreadyExistsError,
    InvalidNicknameError,
    MemberAlreadyExistsError,
    OnboardingData,
    RequiredConsentNotAgreedError,
    TrustedIdentity,
    bootstrap_member,
)


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


@pytest.fixture()
def user_repository(db_session):
    return UserRepository(db_session)


def _row_count(db_session) -> int:
    return len(db_session.execute(select(User)).scalars().all())


def _existing_member(
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


class TestBootstrapMemberSuccess:
    def test_creates_member_with_expected_member_id(self, db_session, user_repository):
        member_id = uuid.uuid4()
        identity = TrustedIdentity(user_id=member_id, email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.member_id == member_id

    def test_email_is_saved(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.email == "new@example.com"

    def test_email_is_normalized_with_strip_and_lowercase(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="  New@Example.COM  ")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.email == "new@example.com"

    def test_nickname_is_saved(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.nickname == "newnick"

    def test_nickname_is_trimmed_before_saving(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="  newnick  ", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.nickname == "newnick"

    def test_status_defaults_to_active(self, db_session, user_repository):
        """CLIAR-87: member 최초 생성 시 status는 ACTIVE로 설정된다
        (PENDING은 최종 스키마에 존재하지 않는다)."""
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.status == MemberStatus.ACTIVE


class TestBootstrapMemberRequiredConsent:
    def test_agree_terms_false_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=False, agree_privacy=True)

        with pytest.raises(RequiredConsentNotAgreedError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0

    def test_agree_privacy_false_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=False)

        with pytest.raises(RequiredConsentNotAgreedError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0


class TestBootstrapMemberNicknameValidation:
    def test_null_nickname_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname=None, agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0

    def test_empty_nickname_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="", agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0

    def test_blank_nickname_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="   ", agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0


class TestBootstrapMemberDuplicates:
    def test_duplicate_member_id_is_rejected(self, db_session, user_repository):
        member_id = uuid.uuid4()
        _existing_member(db_session, member_id=member_id)
        identity = TrustedIdentity(user_id=member_id, email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(MemberAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1

    def test_duplicate_email_is_rejected(self, db_session, user_repository):
        _existing_member(db_session, email="taken@example.com")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="taken@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(EmailAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1

    def test_duplicate_email_is_detected_after_normalization(self, db_session, user_repository):
        _existing_member(db_session, email="user@test.com")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="  USER@Test.com  ")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(EmailAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1

    def test_duplicate_nickname_is_allowed(self, db_session, user_repository):
        """CLIAR-87 확정 요구사항: member.nickname은 UNIQUE 제약이 없으며
        중복을 허용한다. 회원 최초 생성 시에도 다른 회원과 동일한
        nickname으로 정상 생성되어야 한다."""
        _existing_member(db_session, nickname="taken-nick")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="taken-nick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.nickname == "taken-nick"
        assert _row_count(db_session) == 2

    def test_duplicate_nickname_after_trim_is_allowed(self, db_session, user_repository):
        _existing_member(db_session, nickname="taken-nick")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(
            nickname="  taken-nick  ", agree_terms=True, agree_privacy=True
        )

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.nickname == "taken-nick"
        assert _row_count(db_session) == 2
