"""
MEMBER 최초 생성 service(app/services/member_service.py) 테스트.

이번 Jira는 FastAPI endpoint가 없는 순수 service/repository 계층이므로,
TestClient 없이 in-memory SQLite 세션 + UserRepository를 직접 사용해
검증한다. TrustedIdentity(user_id/email)는 이미 인증 계층에서 신뢰된
값이라고 가정하며, 이 테스트에서도 HTTP 헤더나 Cognito 호출을 흉내내지
않고 값을 직접 구성해서 넘긴다.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services.member_service import (
    EmailAlreadyExistsError,
    InvalidNicknameError,
    MemberAlreadyExistsError,
    NicknameAlreadyExistsError,
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


def _existing_member(db_session, user_id="existing-sub", email="existing@example.com", nickname="existing-nick"):
    now = datetime.now(timezone.utc)
    member = User(
        user_id=user_id,
        email=email,
        nickname=nickname,
        agree_terms=True,
        agree_privacy=True,
        agreed_at=now,
    )
    db_session.add(member)
    db_session.commit()
    return member


class TestBootstrapMemberSuccess:
    def test_creates_member_with_expected_user_id(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="cognito-sub-0001", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.user_id == "cognito-sub-0001"

    def test_email_is_saved(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.email == "new@example.com"

    def test_email_is_normalized_with_strip_and_lowercase(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="  New@Example.COM  ")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.email == "new@example.com"

    def test_nickname_is_saved(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.nickname == "newnick"

    def test_nickname_is_trimmed_before_saving(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="  newnick  ", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.nickname == "newnick"

    def test_agree_ai_analysis_false_creates_member(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(
            nickname="newnick", agree_terms=True, agree_privacy=True, agree_ai_analysis=False
        )

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.agree_ai_analysis is False

    def test_agree_ai_analysis_true_creates_member(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(
            nickname="newnick", agree_terms=True, agree_privacy=True, agree_ai_analysis=True
        )

        member = bootstrap_member(identity, onboarding, user_repository)

        assert member.agree_ai_analysis is True

    def test_agreed_at_is_recorded(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        before = datetime.now(timezone.utc)
        member = bootstrap_member(identity, onboarding, user_repository)
        after = datetime.now(timezone.utc)

        assert member.agreed_at is not None
        # SQLite는 DateTime(timezone=True) 컬럼이라도 refresh 이후
        # naive datetime을 반환하는 한계가 있다(실제 PostgreSQL은
        # timezone-aware로 정상 보존한다). 테스트 환경 한계를 감안해
        # naive/aware 여부와 무관하게 값만 비교한다.
        agreed_at = member.agreed_at
        if agreed_at.tzinfo is None:
            agreed_at = agreed_at.replace(tzinfo=timezone.utc)
        assert before <= agreed_at <= after

    def test_status_uses_existing_model_default(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = bootstrap_member(identity, onboarding, user_repository)

        # User ORM의 기존 status default("PENDING")를 그대로 사용하며,
        # 이번 서비스에서 임의로 ACTIVE 등으로 전환하지 않는다.
        assert member.status == "PENDING"


class TestBootstrapMemberRequiredConsent:
    def test_agree_terms_false_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=False, agree_privacy=True)

        with pytest.raises(RequiredConsentNotAgreedError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0

    def test_agree_privacy_false_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=False)

        with pytest.raises(RequiredConsentNotAgreedError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0


class TestBootstrapMemberNicknameValidation:
    def test_null_nickname_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname=None, agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0

    def test_empty_nickname_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="", agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0

    def test_blank_nickname_is_rejected(self, db_session, user_repository):
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="   ", agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 0


class TestBootstrapMemberDuplicates:
    def test_duplicate_user_id_is_rejected(self, db_session, user_repository):
        _existing_member(db_session, user_id="dup-sub")
        identity = TrustedIdentity(user_id="dup-sub", email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(MemberAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1

    def test_duplicate_email_is_rejected(self, db_session, user_repository):
        _existing_member(db_session, email="taken@example.com")
        identity = TrustedIdentity(user_id="sub-1", email="taken@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(EmailAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1

    def test_duplicate_email_is_detected_after_normalization(self, db_session, user_repository):
        _existing_member(db_session, email="user@test.com")
        identity = TrustedIdentity(user_id="sub-1", email="  USER@Test.com  ")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(EmailAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1

    def test_duplicate_nickname_is_rejected(self, db_session, user_repository):
        _existing_member(db_session, nickname="taken-nick")
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(nickname="taken-nick", agree_terms=True, agree_privacy=True)

        with pytest.raises(NicknameAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1

    def test_duplicate_nickname_is_detected_after_trim(self, db_session, user_repository):
        _existing_member(db_session, nickname="taken-nick")
        identity = TrustedIdentity(user_id="sub-1", email="new@example.com")
        onboarding = OnboardingData(
            nickname="  taken-nick  ", agree_terms=True, agree_privacy=True
        )

        with pytest.raises(NicknameAlreadyExistsError):
            bootstrap_member(identity, onboarding, user_repository)

        assert _row_count(db_session) == 1
