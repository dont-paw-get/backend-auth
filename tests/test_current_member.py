"""
Cognito sub 기반 현재 사용자 식별 dependency 테스트.

실제 AWS Cognito/API Gateway 없이도 검증 가능하도록,
- get_current_user_id: header 파싱 로직만 단위 테스트
- get_current_member: in-memory SQLite 세션 + UserRepository를 이용해
  "sub -> MEMBER 조회" 흐름만 단위 테스트한다.

두 dependency 모두 FastAPI에서는 일반 Python 함수이므로,
TestClient 없이 직접 호출해서 테스트할 수 있다.
"""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_member
from app.core.database import Base
from app.core.security import get_current_user_id
from app.models.user import User


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


def _make_member(db_session, user_id="cognito-sub-0001"):
    now = datetime.now(timezone.utc)
    member = User(
        user_id=user_id,
        email="member@example.com",
        nickname="membernick",
        agree_terms=True,
        agree_privacy=True,
        agreed_at=now,
    )
    db_session.add(member)
    db_session.commit()
    return member


class TestGetCurrentUserId:
    def test_raises_error_when_auth_integration_is_not_configured(self):
        """
        API Gateway/Cognito 연동 방식이 아직 확정되지 않았으므로,
        실제 호출 시에는 어떤 사용자 ID도 신뢰해서는 안 되고
        "인증 연동이 구성되지 않음"을 명확한 오류로 표현해야 한다.
        """
        with pytest.raises(HTTPException) as exc_info:
            get_current_user_id()

        assert exc_info.value.status_code == 501


class TestGetCurrentMember:
    def test_returns_member_when_sub_matches_existing_user_id(self, db_session):
        _make_member(db_session, user_id="cognito-sub-0001")

        member = get_current_member(user_id="cognito-sub-0001", db=db_session)

        assert member.user_id == "cognito-sub-0001"
        assert member.email == "member@example.com"

    def test_raises_404_when_member_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            get_current_member(user_id="unknown-sub", db=db_session)

        assert exc_info.value.status_code == 404

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
            return {"user_id": member.user_id}

        _make_member(db_session, user_id="cognito-sub-0001")

        app.dependency_overrides[get_current_user_id] = lambda: "cognito-sub-0001"
        from app.core.database import get_db

        app.dependency_overrides[get_db] = lambda: db_session

        with TestClient(app) as client:
            response = client.get("/whoami")

        app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json() == {"user_id": "cognito-sub-0001"}
