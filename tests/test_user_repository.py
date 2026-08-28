"""
UserRepository의 조회 메서드 테스트.

실제 PostgreSQL에 연결하지 않고 in-memory SQLite에 모델 메타데이터로
테이블을 만들어 검증한다(tests/test_user_model.py와 동일한 방식).

여기서 특히 중요한 것은 status에 따른 필터링 동작이다. BE 주도 인증
전환에서 member는 PENDING(이메일 인증 대기) 상태로 먼저 생성되므로,
"이 이메일/닉네임이 이미 사용 중인가"를 판단하는 메서드들이 PENDING을
어떻게 취급하는지가 회원가입 응답의 일관성을 좌우한다.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.user import MemberStatus, User
from app.repositories.user_repository import UserRepository


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
def repository(db_session):
    return UserRepository(db_session)


def _create_member(
    db_session,
    *,
    email="member@example.com",
    nickname="membernick",
    status=MemberStatus.ACTIVE,
):
    member = User(
        member_id=uuid.uuid4(),
        email=email,
        nickname=nickname,
        status=status,
    )
    db_session.add(member)
    db_session.commit()
    return member


class TestGetByEmail:
    def test_returns_member_when_email_matches(self, db_session, repository):
        created = _create_member(db_session, email="found@example.com")

        found = repository.get_by_email("found@example.com")

        assert found is not None
        assert found.member_id == created.member_id

    def test_returns_none_when_email_does_not_exist(self, repository):
        assert repository.get_by_email("nobody@example.com") is None

    @pytest.mark.parametrize(
        "status",
        [MemberStatus.PENDING, MemberStatus.ACTIVE, MemberStatus.WITHDRAWN],
    )
    def test_returns_member_regardless_of_status(
        self, db_session, repository, status
    ):
        """
        status로 필터링하지 않는다. 상태에 따른 판단은 호출자(service)의
        책임이며, repository는 조회만 담당한다. 특히 회원가입 재시도
        경로에서 PENDING/WITHDRAWN 회원을 찾아낼 수 있어야 한다.
        """
        created = _create_member(db_session, email="any@example.com", status=status)

        found = repository.get_by_email("any@example.com")

        assert found is not None
        assert found.member_id == created.member_id
        assert found.status == status

    def test_does_not_normalize_the_given_email(self, db_session, repository):
        """
        정규화(strip + lower)는 호출자의 책임이라고 문서화되어 있다.
        repository가 몰래 정규화해 주면 호출자가 정규화를 빠뜨려도
        동작해 버려서, 저장 시점과 조회 시점의 정규화 정책이 어긋나는
        것을 잡아낼 수 없다.
        """
        _create_member(db_session, email="cased@example.com")

        assert repository.get_by_email("CASED@example.com") is None


class TestExistsByEmail:
    def test_true_for_active_member(self, db_session, repository):
        _create_member(db_session, email="active@example.com")

        assert repository.exists_by_email("active@example.com") is True

    def test_true_for_pending_member(self, db_session, repository):
        """
        PENDING 회원의 이메일은 Cognito User Pool에서 이미 점유된
        상태다. 같은 이메일로 신규 가입을 시도하면 어차피
        UsernameExistsException으로 실패하므로, 중복 확인 단계에서도
        "사용 중"으로 응답해야 일관된다.
        """
        _create_member(
            db_session, email="pending@example.com", status=MemberStatus.PENDING
        )

        assert repository.exists_by_email("pending@example.com") is True

    def test_false_when_email_does_not_exist(self, repository):
        assert repository.exists_by_email("free@example.com") is False


class TestExistsByNickname:
    def test_true_for_pending_member(self, db_session, repository):
        _create_member(
            db_session,
            email="pending2@example.com",
            nickname="pendingnick",
            status=MemberStatus.PENDING,
        )

        assert repository.exists_by_nickname("pendingnick") is True

    def test_false_when_nickname_does_not_exist(self, repository):
        assert repository.exists_by_nickname("freenick") is False
