"""
Cognito sub 기반 현재 사용자 식별 dependency 테스트.

실제 AWS Cognito/API Gateway 없이도 검증 가능하도록,
- get_current_user_id: header 파싱 로직만 단위 테스트
- get_current_member: in-memory SQLite 세션 + UserRepository를 이용해
  "sub -> MEMBER 조회" 흐름만 단위 테스트한다.

두 dependency 모두 FastAPI에서는 일반 Python 함수이므로,
TestClient 없이 직접 호출해서 테스트할 수 있다.

CLIAR-87: member.member_id는 UUID이므로, Cognito sub 문자열도 UUID
형식이어야 한다. get_current_member는 이 문자열을 UUID로 파싱한다.
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_member
from app.core.database import Base
from app.core.security import get_current_user_id
from app.models.user import MemberStatus, User


@pytest.fixture()
def engine():
    # StaticPool + check_same_thread=False: TestClient가 별도 스레드에서
    # 요청을 처리하더라도 동일한 in-memory SQLite 연결을 재사용하게 한다.
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


def _make_member(db_session, member_id, *, status=MemberStatus.ACTIVE):
    member = User(
        member_id=member_id,
        email="member@example.com",
        nickname="membernick",
        status=status,
    )
    db_session.add(member)
    db_session.commit()
    return member


class TestGetCurrentUserId:
    def test_raises_401_when_called_without_authorization(self):
        """
        CLIAR-105: Cognito 인증 연동이 실제로 구현되었으므로, dependency
        주입 없이 이 함수를 직접 호출하는 경우(Authorization 헤더가
        없는 것과 동일하게 취급)도 더 이상 "인증 연동 미구성"(501,
        CLIAR-71 시절의 임시 정책)이 아니라 일반적인 인증 실패(401)로
        취급해야 한다.
        """
        with pytest.raises(HTTPException) as exc_info:
            get_current_user_id()

        assert exc_info.value.status_code == 401


class TestGetCurrentMember:
    def test_returns_member_when_sub_matches_existing_member_id(self, db_session):
        member_id = uuid.uuid4()
        _make_member(db_session, member_id)

        member = get_current_member(user_id=str(member_id), db=db_session)

        assert member.member_id == member_id
        assert member.email == "member@example.com"

    def test_raises_404_when_member_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            get_current_member(user_id=str(uuid.uuid4()), db=db_session)

        assert exc_info.value.status_code == 404

    def test_raises_401_when_sub_is_not_a_valid_uuid(self, db_session):
        """
        Cognito sub가 UUID 형식이 아니면(예: 예상과 다른 인증 연동 상태)
        임의의 값으로 대체하지 않고 명확히 401로 실패해야 한다.
        """
        with pytest.raises(HTTPException) as exc_info:
            get_current_member(user_id="not-a-uuid", db=db_session)

        assert exc_info.value.status_code == 401

    def test_raises_403_when_member_is_pending(self, db_session):
        """
        PENDING = Cognito SignUp은 됐지만 이메일 인증(ConfirmSignUp)이
        끝나지 않은 상태. 유효한 access token을 들고 왔더라도 일반 API
        접근은 403으로 차단해야 한다.

        정상 경로에서는 Cognito가 미확인 계정의 InitiateAuth를 거부하므로
        PENDING 회원은 토큰을 얻을 수 없다. 다만 ConfirmSignUp 성공 후
        DB UPDATE가 실패해 Cognito=CONFIRMED / DB=PENDING으로 어긋난
        경우가 있을 수 있어 이 방어가 필요하다.
        """
        member_id = uuid.uuid4()
        _make_member(db_session, member_id, status=MemberStatus.PENDING)

        with pytest.raises(HTTPException) as exc_info:
            get_current_member(user_id=str(member_id), db=db_session)

        assert exc_info.value.status_code == 403

    def test_pending_403_carries_email_not_verified_code(self, db_session):
        """
        FE가 "탈퇴한 계정"과 "이메일 인증 미완료"를 구분해서 라우팅할 수
        있어야 하므로, PENDING의 403은 기계가 읽을 수 있는 code를
        포함해야 한다.
        """
        member_id = uuid.uuid4()
        _make_member(db_session, member_id, status=MemberStatus.PENDING)

        with pytest.raises(HTTPException) as exc_info:
            get_current_member(user_id=str(member_id), db=db_session)

        assert exc_info.value.detail["code"] == "EMAIL_NOT_VERIFIED"

    def test_withdrawn_takes_precedence_over_pending(self, db_session):
        """
        탈퇴는 종착 상태다. deleted_at이 찍힌 PENDING row가 존재하더라도
        "이메일 인증 필요"가 아니라 "탈퇴한 계정"으로 응답해야 한다.
        """
        from datetime import datetime, timezone

        member_id = uuid.uuid4()
        member = _make_member(db_session, member_id, status=MemberStatus.PENDING)
        member.deleted_at = datetime.now(timezone.utc)
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            get_current_member(user_id=str(member_id), db=db_session)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "This member has been withdrawn"

    def test_dependency_can_be_overridden_in_fastapi_app(self, db_session):
        """
        get_current_user_id / get_current_member는 FastAPI dependency이므로
        app.dependency_overrides로 손쉽게 mock 가능해야 한다.
        실제 앱에 아직 이 dependency를 사용하는 라우터가 없으므로,
        여기서는 override 메커니즘 자체가 정상 동작하는지를
        임시 FastAPI 앱으로 검증한다.
        """
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/whoami")
        def whoami(member: User = Depends(get_current_member)):
            return {"member_id": str(member.member_id)}

        member_id = uuid.uuid4()
        _make_member(db_session, member_id)

        app.dependency_overrides[get_current_user_id] = lambda: str(member_id)
        from app.core.database import get_db

        app.dependency_overrides[get_db] = lambda: db_session

        with TestClient(app) as client:
            response = client.get("/whoami")

        app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json() == {"member_id": str(member_id)}
