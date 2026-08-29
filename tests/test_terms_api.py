"""
GET /api/v1/terms 테스트 (CLIAR-176).

FE가 회원가입 화면에서 약관 원문을 보여줄 수 있도록 공개(인증 없음)
목록 조회 API를 제공한다. 실제 AWS/Cognito는 호출하지 않는다(순수
DB 조회 endpoint).

정책 결정(완료 보고에도 기록, 최초 구현 이후 재검토 반영):
- 필수 약관(TERMS_OF_SERVICE, PRIVACY) 중 하나라도 현재 적용 중인
  행이 없으면 503을 반환한다 — POST /api/v1/auth/signup이 동일한
  상황에서 사용하는 RequiredTermsNotConfiguredError
  (app/services/member_service.py)를 그대로 재사용한다(같은 의미의
  새 예외를 만들지 않음).
- AI_ANALYSIS는 선택 약관이므로 없어도 503을 만들지 않고 200 +
  나머지 목록을 그대로 반환한다(signup도 agree_ai_analysis=true일
  때만 이 약관을 요구하므로 동일한 정책).
- 반환 순서는 TERMS_OF_SERVICE -> PRIVACY -> AI_ANALYSIS로 고정한다.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models.terms import Terms

ENDPOINT = "/api/v1/terms"


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


def _seed(
    db_session,
    *,
    code,
    name=None,
    content=None,
    is_required=False,
    effective_at=None,
    expired_at=None,
    deleted_at=None,
):
    now = datetime.now(timezone.utc)
    terms = Terms(
        code=code,
        name=name or code,
        content=content or f"{code} content",
        is_required=is_required,
        effective_at=effective_at or (now - timedelta(days=1)),
        expired_at=expired_at,
        deleted_at=deleted_at,
    )
    db_session.add(terms)
    db_session.commit()
    return terms


def _seed_all_current(db_session):
    _seed(db_session, code="TERMS_OF_SERVICE", name="서비스 이용약관", is_required=True)
    _seed(db_session, code="PRIVACY", name="개인정보 관련 약관", is_required=True)
    _seed(db_session, code="AI_ANALYSIS", name="AI 분석 동의", is_required=False)


class TestListTermsSuccess:
    def test_returns_200(self, client, db_session):
        _seed_all_current(db_session)

        response = client.get(ENDPOINT)

        assert response.status_code == 200

    def test_no_authorization_header_required(self, client, db_session):
        """공개 endpoint이므로 Authorization 헤더 없이 호출 가능해야
        한다."""
        _seed_all_current(db_session)

        response = client.get(ENDPOINT)

        assert response.status_code == 200

    def test_response_field_contract(self, client, db_session):
        _seed(
            db_session,
            code="TERMS_OF_SERVICE",
            name="서비스 이용약관",
            content="본문 내용입니다",
            is_required=True,
        )
        _seed(db_session, code="PRIVACY", name="개인정보 관련 약관", is_required=True)

        body = client.get(ENDPOINT).json()

        assert body[0] == {
            "code": "TERMS_OF_SERVICE",
            "name": "서비스 이용약관",
            "content": "본문 내용입니다",
            "is_required": True,
        }

    def test_internal_fields_are_not_exposed(self, client, db_session):
        """id/effective_at/expired_at/created_at/updated_at/deleted_at
        등 내부 관리용 필드는 응답에 포함되지 않는다."""
        _seed(db_session, code="TERMS_OF_SERVICE", is_required=True)
        _seed(db_session, code="PRIVACY", is_required=True)

        body = client.get(ENDPOINT).json()[0]

        assert set(body.keys()) == {"code", "name", "content", "is_required"}

    def test_is_required_flag_reflects_db_value(self, client, db_session):
        _seed(db_session, code="TERMS_OF_SERVICE", is_required=True)
        _seed(db_session, code="PRIVACY", is_required=True)
        _seed(db_session, code="AI_ANALYSIS", is_required=False)

        body = client.get(ENDPOINT).json()
        by_code = {row["code"]: row["is_required"] for row in body}

        assert by_code["TERMS_OF_SERVICE"] is True
        assert by_code["AI_ANALYSIS"] is False

    def test_order_is_terms_of_service_then_privacy_then_ai_analysis(
        self, client, db_session
    ):
        """DB insert 순서와 무관하게 응답 순서는 항상 고정되어야
        한다."""
        _seed(db_session, code="AI_ANALYSIS")
        _seed(db_session, code="PRIVACY")
        _seed(db_session, code="TERMS_OF_SERVICE")

        body = client.get(ENDPOINT).json()

        assert [row["code"] for row in body] == [
            "TERMS_OF_SERVICE",
            "PRIVACY",
            "AI_ANALYSIS",
        ]

    def test_unknown_code_is_appended_after_the_known_three(
        self, client, db_session
    ):
        _seed(db_session, code="MARKETING")
        _seed(db_session, code="PRIVACY")
        _seed(db_session, code="TERMS_OF_SERVICE")

        body = client.get(ENDPOINT).json()

        assert [row["code"] for row in body] == [
            "TERMS_OF_SERVICE",
            "PRIVACY",
            "MARKETING",
        ]


class TestListTermsCurrentFiltering:
    """
    "현재 적용 중"의 기준은 app/repositories/terms_repository.py의
    get_current_by_code와 완전히 동일해야 한다(signup이 참조하는
    약관과 이 목록이 어긋나면 안 되므로).

    TERMS_OF_SERVICE가 필터링으로 제외되면(만료/삭제/미래 시행)
    필수 약관 관점에서는 "없는 것"과 동일하므로, 아래 세 테스트는
    이제 200이 아니라 503을 기대한다(정책 변경 반영). PRIVACY는
    항상 정상 상태로 함께 심어서, 이 테스트들이 TERMS_OF_SERVICE의
    시간 필터링만 독립적으로 검증하도록 한다.
    """

    def test_expired_terms_are_excluded(self, client, db_session):
        now = datetime.now(timezone.utc)
        _seed(db_session, code="PRIVACY", is_required=True)
        _seed(
            db_session,
            code="TERMS_OF_SERVICE",
            is_required=True,
            effective_at=now - timedelta(days=10),
            expired_at=now - timedelta(days=1),
        )

        response = client.get(ENDPOINT)

        assert response.status_code == 503

    def test_deleted_terms_are_excluded(self, client, db_session):
        now = datetime.now(timezone.utc)
        _seed(db_session, code="PRIVACY", is_required=True)
        _seed(
            db_session,
            code="TERMS_OF_SERVICE",
            is_required=True,
            deleted_at=now - timedelta(days=1),
        )

        response = client.get(ENDPOINT)

        assert response.status_code == 503

    def test_future_effective_terms_are_excluded(self, client, db_session):
        now = datetime.now(timezone.utc)
        _seed(db_session, code="PRIVACY", is_required=True)
        _seed(
            db_session,
            code="TERMS_OF_SERVICE",
            is_required=True,
            effective_at=now + timedelta(days=1),
        )

        response = client.get(ENDPOINT)

        assert response.status_code == 503

    def test_most_recent_effective_row_is_used_when_history_exists(
        self, client, db_session
    ):
        """같은 code로 과거 이력(만료된 행)이 남아 있어도, 현재
        유효한 최신 행만 반환해야 한다."""
        now = datetime.now(timezone.utc)
        _seed(db_session, code="PRIVACY", is_required=True)
        _seed(
            db_session,
            code="TERMS_OF_SERVICE",
            is_required=True,
            content="old content",
            effective_at=now - timedelta(days=30),
            expired_at=now - timedelta(days=10),
        )
        _seed(
            db_session,
            code="TERMS_OF_SERVICE",
            is_required=True,
            content="new content",
            effective_at=now - timedelta(days=9),
        )

        response = client.get(ENDPOINT)
        body = response.json()

        assert response.status_code == 200
        tos_rows = [row for row in body if row["code"] == "TERMS_OF_SERVICE"]
        assert len(tos_rows) == 1
        assert tos_rows[0]["content"] == "new content"


class TestListTermsMissingData:
    """
    필수 약관(TERMS_OF_SERVICE, PRIVACY) 누락 정책 (재검토 후 확정).

    signup(app/services/signup_service.py)이 동일 상황에서 이미
    RequiredTermsNotConfiguredError -> 503으로 매핑하고 있으므로,
    이 조회 endpoint도 같은 예외를 재사용해 동일하게 503을
    반환한다 — 회원가입 화면에 필수 약관을 못 띄우는 서버 설정
    문제를 signup과 이 목록 조회가 서로 다른 상태 코드로 보고하지
    않도록 하기 위함이다. AI_ANALYSIS는 선택 약관이므로 그 부재
    자체는 503을 유발하지 않는다.
    """

    def test_both_required_terms_present_returns_200(self, client, db_session):
        _seed(db_session, code="TERMS_OF_SERVICE", is_required=True)
        _seed(db_session, code="PRIVACY", is_required=True)

        response = client.get(ENDPOINT)

        assert response.status_code == 200
        assert [row["code"] for row in response.json()] == [
            "TERMS_OF_SERVICE",
            "PRIVACY",
        ]

    def test_missing_terms_of_service_returns_503(self, client, db_session):
        _seed(db_session, code="PRIVACY", is_required=True)

        response = client.get(ENDPOINT)

        assert response.status_code == 503

    def test_missing_privacy_returns_503(self, client, db_session):
        _seed(db_session, code="TERMS_OF_SERVICE", is_required=True)

        response = client.get(ENDPOINT)

        assert response.status_code == 503

    def test_missing_ai_analysis_only_returns_200(self, client, db_session):
        """AI_ANALYSIS는 선택 약관이므로, 두 필수 약관만 있으면
        AI_ANALYSIS가 없어도 200을 유지하고 나머지 목록만
        반환한다 — 반환 순서/필드도 그대로 유지된다."""
        _seed(
            db_session,
            code="TERMS_OF_SERVICE",
            name="서비스 이용약관",
            content="이용약관 본문",
            is_required=True,
        )
        _seed(
            db_session,
            code="PRIVACY",
            name="개인정보 관련 약관",
            content="개인정보 본문",
            is_required=True,
        )

        response = client.get(ENDPOINT)

        assert response.status_code == 200
        body = response.json()
        assert [row["code"] for row in body] == ["TERMS_OF_SERVICE", "PRIVACY"]
        assert body[0] == {
            "code": "TERMS_OF_SERVICE",
            "name": "서비스 이용약관",
            "content": "이용약관 본문",
            "is_required": True,
        }
        assert set(body[0].keys()) == {"code", "name", "content", "is_required"}

    def test_empty_terms_table_returns_503(self, client, db_session):
        """두 필수 약관이 모두 없는 극단적인 경우(빈 테이블)도 같은
        RequiredTermsNotConfiguredError 경로로 503을 반환한다."""
        response = client.get(ENDPOINT)

        assert response.status_code == 503

    def test_missing_required_terms_still_requires_no_authorization(
        self, client, db_session
    ):
        """503 응답 경로에서도 인증 없이 호출 가능해야 한다(인증
        관련 401/403이 아니라 순수 설정 문제로서의 503이어야
        한다)."""
        response = client.get(ENDPOINT)

        assert response.status_code == 503
        assert response.status_code not in (401, 403)


class TestOpenApiContract:
    def test_terms_endpoint_is_public_in_openapi(self):
        spec = app.openapi()
        operation = spec["paths"]["/api/v1/terms"]["get"]

        assert "security" not in operation

    def test_existing_auth_paths_are_unaffected(self):
        spec = app.openapi()
        paths = spec["paths"]

        for path in [
            "/api/v1/auth/signup",
            "/api/v1/auth/signup/confirm",
            "/api/v1/auth/signup/resend",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
            "/api/v1/auth/password/forgot",
            "/api/v1/auth/password/reset",
            "/api/v1/auth/password/change",
            "/api/v1/users/me",
        ]:
            assert path in paths

        assert "/api/v1/users/bootstrap" not in paths
