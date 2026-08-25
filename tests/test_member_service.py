"""
MEMBER 최초 생성 service(app/services/member_service.py) 테스트.

이번 Jira는 FastAPI endpoint가 없는 순수 service/repository 계층이므로,
TestClient 없이 in-memory SQLite 세션 + UserRepository를 직접 사용해
검증한다. TrustedIdentity(user_id/email)는 이미 인증 계층에서 신뢰된
값이라고 가정하며, 이 테스트에서도 HTTP 헤더나 Cognito 호출을 흉내내지
않고 값을 직접 구성해서 넘긴다.

CLIAR-87: TrustedIdentity.user_id는 UUID이고, 생성된 member는
member_id(UUID)를 가진다. agree_terms/agree_privacy 필수 동의 검증은
유지되지만, member 테이블에는 더 이상 이 값들이 저장되지 않는다.

CLIAR-92: 회원 최초 생성 성공 시 TERMS_OF_SERVICE/PRIVACY(필수)와
AI_ANALYSIS(선택, agree_ai_analysis=true인 경우만)에 대한 AGREE 이력을
member_agreement에 저장한다. 필수 약관이 DB에 현재 적용 중인 상태로
없으면 member 생성 자체를 롤백해야 한다.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.member_agreement import MemberAgreement, MemberAgreementAction
from app.models.terms import Terms
from app.models.user import MemberStatus, User
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository
from app.services.member_service import (
    EmailAlreadyExistsError,
    InvalidNicknameError,
    MemberAlreadyExistsError,
    OnboardingData,
    RequiredConsentNotAgreedError,
    RequiredTermsNotConfiguredError,
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


@pytest.fixture()
def terms_repository(db_session):
    return TermsRepository(db_session)


@pytest.fixture()
def member_agreement_repository(db_session):
    return MemberAgreementRepository(db_session)


def _row_count(db_session) -> int:
    return len(db_session.execute(select(User)).scalars().all())


def _agreement_count(db_session) -> int:
    return len(db_session.execute(select(MemberAgreement)).scalars().all())


def _agreements_for(db_session, member_id):
    stmt = select(MemberAgreement).where(MemberAgreement.member_id == member_id)
    return db_session.execute(stmt).scalars().all()


def _terms_code_of(db_session, terms_id):
    return db_session.get(Terms, terms_id).code


def _seed_terms(
    db_session,
    code,
    *,
    effective_at=None,
    expired_at=None,
    deleted_at=None,
):
    now = datetime.now(timezone.utc)
    terms = Terms(
        code=code,
        name=code,
        content=f"{code} content",
        is_required=False,
        effective_at=effective_at or (now - timedelta(days=1)),
        expired_at=expired_at,
        deleted_at=deleted_at,
    )
    db_session.add(terms)
    db_session.commit()
    return terms


def _seed_required_terms(db_session):
    """TERMS_OF_SERVICE, PRIVACY를 현재 적용 중인 상태로 생성한다."""
    _seed_terms(db_session, "TERMS_OF_SERVICE")
    _seed_terms(db_session, "PRIVACY")


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


def _bootstrap(identity, onboarding, user_repository, terms_repository, member_agreement_repository):
    return bootstrap_member(
        identity,
        onboarding,
        user_repository,
        terms_repository,
        member_agreement_repository,
    )


class TestBootstrapMemberSuccess:
    def test_creates_member_with_expected_member_id(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        member_id = uuid.uuid4()
        identity = TrustedIdentity(user_id=member_id, email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.member_id == member_id

    def test_email_is_saved(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.email == "new@example.com"

    def test_email_is_normalized_with_strip_and_lowercase(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="  New@Example.COM  ")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.email == "new@example.com"

    def test_nickname_is_saved(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.nickname == "newnick"

    def test_nickname_is_trimmed_before_saving(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="  newnick  ", agree_terms=True, agree_privacy=True)

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.nickname == "newnick"

    def test_status_defaults_to_active(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        """CLIAR-87: member 최초 생성 시 status는 ACTIVE로 설정된다
        (PENDING은 최종 스키마에 존재하지 않는다)."""
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.status == MemberStatus.ACTIVE


class TestBootstrapMemberRequiredConsent:
    def test_agree_terms_false_is_rejected(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=False, agree_privacy=True)

        with pytest.raises(RequiredConsentNotAgreedError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0
        assert _agreement_count(db_session) == 0

    def test_agree_privacy_false_is_rejected(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=False)

        with pytest.raises(RequiredConsentNotAgreedError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0
        assert _agreement_count(db_session) == 0


class TestBootstrapMemberNicknameValidation:
    def test_null_nickname_is_rejected(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname=None, agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0

    def test_empty_nickname_is_rejected(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="", agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0

    def test_blank_nickname_is_rejected(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="   ", agree_terms=True, agree_privacy=True)

        with pytest.raises(InvalidNicknameError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0


class TestBootstrapMemberDuplicates:
    def test_duplicate_member_id_is_rejected(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        member_id = uuid.uuid4()
        _existing_member(db_session, member_id=member_id)
        identity = TrustedIdentity(user_id=member_id, email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(MemberAlreadyExistsError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 1

    def test_duplicate_email_is_rejected(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        _existing_member(db_session, email="taken@example.com")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="taken@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(EmailAlreadyExistsError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 1

    def test_duplicate_email_is_detected_after_normalization(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        _existing_member(db_session, email="user@test.com")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="  USER@Test.com  ")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(EmailAlreadyExistsError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 1

    def test_duplicate_nickname_is_allowed(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        """CLIAR-87 확정 요구사항: member.nickname은 UNIQUE 제약이 없으며
        중복을 허용한다. 회원 최초 생성 시에도 다른 회원과 동일한
        nickname으로 정상 생성되어야 한다."""
        _seed_required_terms(db_session)
        _existing_member(db_session, nickname="taken-nick")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="taken-nick", agree_terms=True, agree_privacy=True)

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.nickname == "taken-nick"
        assert _row_count(db_session) == 2

    def test_duplicate_nickname_after_trim_is_allowed(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        _existing_member(db_session, nickname="taken-nick")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(
            nickname="  taken-nick  ", agree_terms=True, agree_privacy=True
        )

        member = _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        assert member.nickname == "taken-nick"
        assert _row_count(db_session) == 2


class TestBootstrapMemberAgreementCreation:
    """CLIAR-92: 회원 최초 생성 시 member_agreement AGREE 이력 생성."""

    def test_required_terms_present_creates_two_agreements(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        member_id = uuid.uuid4()
        identity = TrustedIdentity(user_id=member_id, email="new@example.com")
        onboarding = OnboardingData(
            nickname="newnick", agree_terms=True, agree_privacy=True, agree_ai_analysis=False
        )

        _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        agreements = _agreements_for(db_session, member_id)
        assert len(agreements) == 2
        codes = {_terms_code_of(db_session, a.terms_id) for a in agreements}
        assert codes == {"TERMS_OF_SERVICE", "PRIVACY"}
        assert all(a.action == MemberAgreementAction.AGREE for a in agreements)

    def test_ai_analysis_true_creates_third_agreement(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        _seed_terms(db_session, "AI_ANALYSIS")
        member_id = uuid.uuid4()
        identity = TrustedIdentity(user_id=member_id, email="new@example.com")
        onboarding = OnboardingData(
            nickname="newnick", agree_terms=True, agree_privacy=True, agree_ai_analysis=True
        )

        _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        agreements = _agreements_for(db_session, member_id)
        codes = {_terms_code_of(db_session, a.terms_id) for a in agreements}
        assert codes == {"TERMS_OF_SERVICE", "PRIVACY", "AI_ANALYSIS"}
        assert len(agreements) == 3
        assert all(a.action == MemberAgreementAction.AGREE for a in agreements)

    def test_ai_analysis_false_creates_no_ai_agreement(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_required_terms(db_session)
        _seed_terms(db_session, "AI_ANALYSIS")
        member_id = uuid.uuid4()
        identity = TrustedIdentity(user_id=member_id, email="new@example.com")
        onboarding = OnboardingData(
            nickname="newnick", agree_terms=True, agree_privacy=True, agree_ai_analysis=False
        )

        _bootstrap(
            identity, onboarding, user_repository, terms_repository, member_agreement_repository
        )

        agreements = _agreements_for(db_session, member_id)
        codes = {_terms_code_of(db_session, a.terms_id) for a in agreements}
        assert codes == {"TERMS_OF_SERVICE", "PRIVACY"}
        assert "AI_ANALYSIS" not in codes

    def test_missing_terms_of_service_rolls_back_member(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        # TERMS_OF_SERVICE를 세팅하지 않고 PRIVACY만 세팅한다.
        _seed_terms(db_session, "PRIVACY")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(RequiredTermsNotConfiguredError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0
        assert _agreement_count(db_session) == 0

    def test_missing_privacy_rolls_back_member(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        _seed_terms(db_session, "TERMS_OF_SERVICE")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(RequiredTermsNotConfiguredError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0
        assert _agreement_count(db_session) == 0

    def test_ai_analysis_true_but_missing_terms_rolls_back_everything(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        # 필수 약관은 모두 있지만 AI_ANALYSIS 약관이 없는 상태.
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(
            nickname="newnick", agree_terms=True, agree_privacy=True, agree_ai_analysis=True
        )

        with pytest.raises(RequiredTermsNotConfiguredError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0
        assert _agreement_count(db_session) == 0

    def test_expired_terms_is_not_treated_as_current(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        now = datetime.now(timezone.utc)
        _seed_terms(
            db_session,
            "TERMS_OF_SERVICE",
            effective_at=now - timedelta(days=10),
            expired_at=now - timedelta(days=1),
        )
        _seed_terms(db_session, "PRIVACY")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(RequiredTermsNotConfiguredError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0

    def test_deleted_terms_is_not_treated_as_current(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        now = datetime.now(timezone.utc)
        _seed_terms(
            db_session,
            "TERMS_OF_SERVICE",
            deleted_at=now - timedelta(days=1),
        )
        _seed_terms(db_session, "PRIVACY")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(RequiredTermsNotConfiguredError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0

    def test_future_effective_terms_is_not_treated_as_current(
        self, db_session, user_repository, terms_repository, member_agreement_repository
    ):
        now = datetime.now(timezone.utc)
        _seed_terms(
            db_session,
            "TERMS_OF_SERVICE",
            effective_at=now + timedelta(days=1),
        )
        _seed_terms(db_session, "PRIVACY")
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        with pytest.raises(RequiredTermsNotConfiguredError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0

    def test_agreement_failure_rolls_back_member(
        self, db_session, user_repository, terms_repository, member_agreement_repository, monkeypatch
    ):
        """agreement 저장 중 예외가 발생하면 member까지 rollback되는지 검증한다."""
        _seed_required_terms(db_session)
        identity = TrustedIdentity(user_id=uuid.uuid4(), email="new@example.com")
        onboarding = OnboardingData(nickname="newnick", agree_terms=True, agree_privacy=True)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure while saving agreement")

        monkeypatch.setattr(member_agreement_repository, "create", _boom)

        with pytest.raises(RuntimeError):
            _bootstrap(
                identity,
                onboarding,
                user_repository,
                terms_repository,
                member_agreement_repository,
            )

        assert _row_count(db_session) == 0
        assert _agreement_count(db_session) == 0
