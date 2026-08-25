"""
POST /api/v1/users/bootstrap 테스트

최초 MEMBER 생성 API 검증.

CLIAR-105: bootstrap은 client request body의 user_id/email을 신뢰하지
않는다. Authorization: Bearer <Cognito Access Token>을 검증하고, sub는
JWT payload에서, email은 Cognito GetUser 응답에서 얻는다. 실제
Cognito/AWS에 접속하지 않기 위해 app.core.cognito.verify_cognito_token과
get_cognito_user_email을 monkeypatch한다.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import users as users_api
from app.core import security
from app.core.database import Base, get_db
from app.main import app
from app.models.member_agreement import MemberAgreement
from app.models.terms import Terms
from app.models.user import MemberStatus, User


ENDPOINT = "/api/v1/users/bootstrap"


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


def _create_member(
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


def _seed_terms(db_session, code):
    now = datetime.now(timezone.utc)
    terms = Terms(
        code=code,
        name=code,
        content=f"{code} content",
        is_required=False,
        effective_at=now - timedelta(days=1),
    )
    db_session.add(terms)
    db_session.commit()
    return terms


def _seed_required_terms(db_session):
    """TERMS_OF_SERVICE, PRIVACY를 현재 적용 중인 상태로 생성한다."""
    _seed_terms(db_session, "TERMS_OF_SERVICE")
    _seed_terms(db_session, "PRIVACY")


def _authenticate_as(monkeypatch, sub, email="test@example.com", token="fake-access-token"):
    """
    Authorization 헤더로 넘길 access token 문자열과, 그 token으로 검증했을
    때 나와야 하는 sub/email을 monkeypatch로 고정한다. 실제 JWKS/Cognito
    네트워크 호출을 하지 않는다.
    """

    def _fake_verify(received_token):
        assert received_token == token
        return {"sub": sub, "token_use": "access"}

    def _fake_get_email(received_token):
        assert received_token == token
        return email

    # security.py/users.py는 `from app.core.cognito import ...`로 직접
    # import하므로, cognito 모듈의 속성을 patch해도 이미 바인딩된
    # 참조에는 영향이 없다. 실제로 사용되는 이름(security.py의
    # verify_cognito_token, app/api/users.py의 get_cognito_user_email)을
    # 직접 patch한다.
    monkeypatch.setattr(security, "verify_cognito_token", _fake_verify)
    monkeypatch.setattr(users_api, "get_cognito_user_email", _fake_get_email)
    return {"Authorization": f"Bearer {token}"}


class TestBootstrapMember:

    def test_creates_member_successfully(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        member_id = str(uuid.uuid4())
        headers = _authenticate_as(monkeypatch, sub=member_id, email="test@example.com")

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_ai_analysis": False,
            },
        )

        assert response.status_code == 201

        body = response.json()

        assert body["member_id"] == member_id
        assert body["email"] == "test@example.com"
        assert body["nickname"] == "haechan"

    def test_member_is_saved_in_database(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        member_id = str(uuid.uuid4())
        headers = _authenticate_as(monkeypatch, sub=member_id, email="test@example.com")

        client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        member = db_session.query(User).first()

        assert member is not None
        assert str(member.member_id) == member_id

    def test_verified_sub_is_stored_as_member_id(self, client, db_session, monkeypatch):
        """client가 body로 identity를 보낼 수 없으므로, 저장되는
        member_id는 오직 검증된 Access Token의 sub에서 나온다."""
        _seed_required_terms(db_session)
        member_id = str(uuid.uuid4())
        headers = _authenticate_as(monkeypatch, sub=member_id, email="whatever@example.com")

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 201
        assert response.json()["member_id"] == member_id

    def test_cognito_email_is_stored_as_member_email(self, client, db_session, monkeypatch):
        """member.email은 Cognito GetUser가 반환한 값이어야 한다."""
        _seed_required_terms(db_session)
        headers = _authenticate_as(
            monkeypatch, sub=str(uuid.uuid4()), email="verified-from-cognito@example.com"
        )

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 201
        assert response.json()["email"] == "verified-from-cognito@example.com"


class TestBootstrapAuthorization:
    """CLIAR-105: Authorization 없음/위조/만료/잘못된 issuer 등의 인증 오류."""

    def test_missing_authorization_returns_401(self, client, db_session):
        """
        CLIAR-105: Cognito 인증 연동이 실제로 구현되었으므로, Authorization
        헤더 자체가 없는 경우도 더 이상 "인증 연동 미구성"(501, CLIAR-71
        시절의 임시 정책)이 아니라 일반적인 인증 실패(401)로 취급한다.
        """
        _seed_required_terms(db_session)

        response = client.post(
            ENDPOINT,
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401

    def test_sub_is_not_a_valid_uuid_returns_401(self, client, db_session, monkeypatch):
        """
        CLIAR-87부터 member.member_id는 UUID 컬럼이다. Cognito sub가
        UUID 형식이 아니면(예: 예상과 다른 인증 연동 상태) 임의의 값으로
        대체하지 않고 401로 명확히 실패해야 한다(app/api/users.py의
        uuid.UUID(user_id) 파싱 참고). 구 CLIAR-87 시절
        test_non_uuid_user_id_returns_422(request body의 user_id 검증)가
        CLIAR-105에서 request body의 user_id 필드 제거로 인해 더 이상
        성립하지 않게 되었으므로, 동일한 개념을 "인증된 sub가 UUID가
        아닌 경우"로 대체한다.
        """
        _seed_required_terms(db_session)
        headers = _authenticate_as(monkeypatch, sub="not-a-uuid")

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401
        assert db_session.query(User).count() == 0

    def test_malformed_bearer_header_returns_401(self, client, db_session):
        _seed_required_terms(db_session)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "NotBearer sometoken"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401

    def test_invalid_or_forged_token_returns_401(self, client, db_session, monkeypatch):
        def _reject(_token):
            raise ValueError("Invalid Cognito token")

        monkeypatch.setattr(security, "verify_cognito_token", _reject)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer forged-token"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401

    def test_id_token_returns_401(self, client, db_session, monkeypatch):
        """token_use=id인 ID Token은 API 인증 토큰으로 허용하지 않는다.
        verify_cognito_token 자체가 이를 거절하므로(cognito.py 참고),
        여기서는 그 거절이 401로 이어지는지 endpoint 레벨에서 재확인한다."""

        def _reject_id_token(_token):
            raise ValueError("Only Cognito Access Tokens are accepted")

        monkeypatch.setattr(security, "verify_cognito_token", _reject_id_token)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer id-token"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401

    def test_expired_token_returns_401(self, client, db_session, monkeypatch):
        def _reject_expired(_token):
            raise ValueError("Invalid Cognito token")

        monkeypatch.setattr(security, "verify_cognito_token", _reject_expired)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer expired-token"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401

    def test_wrong_issuer_returns_401(self, client, db_session, monkeypatch):
        def _reject_issuer(_token):
            raise ValueError("Invalid Cognito token")

        monkeypatch.setattr(security, "verify_cognito_token", _reject_issuer)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer wrong-issuer-token"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401

    def test_wrong_client_id_returns_401(self, client, db_session, monkeypatch):
        def _reject_client_id(_token):
            raise ValueError("Token was not issued for this Cognito App Client")

        monkeypatch.setattr(security, "verify_cognito_token", _reject_client_id)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer wrong-client-token"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401

    def test_getuser_rejection_returns_401_and_no_member_created(
        self, client, db_session, monkeypatch
    ):
        """
        JWT 자체는 유효해도 Cognito GetUser가 토큰을 거절하면(만료/폐기
        등) 401을 반환하고 member row가 생성되지 않아야 한다.
        """
        _seed_required_terms(db_session)

        def _fake_verify(token):
            return {"sub": str(uuid.uuid4()), "token_use": "access"}

        def _fake_get_email_rejects(token):
            raise ValueError("Cognito rejected the access token")

        monkeypatch.setattr(security, "verify_cognito_token", _fake_verify)
        monkeypatch.setattr(users_api, "get_cognito_user_email", _fake_get_email_rejects)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer some-token"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code == 401
        assert db_session.query(User).count() == 0

    def test_cognito_outage_returns_5xx_and_no_member_created(
        self, client, db_session, monkeypatch
    ):
        """Cognito와의 통신 자체가 실패하면(일시 장애) 내부 정보를
        노출하지 않는 5xx를 반환하고 member row가 생성되지 않아야 한다."""
        _seed_required_terms(db_session)

        def _fake_verify(token):
            return {"sub": str(uuid.uuid4()), "token_use": "access"}

        def _fake_get_email_outage(token):
            raise RuntimeError("Could not reach Cognito")

        monkeypatch.setattr(security, "verify_cognito_token", _fake_verify)
        monkeypatch.setattr(users_api, "get_cognito_user_email", _fake_get_email_outage)

        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer some-token"},
            json={"nickname": "haechan", "agree_terms": True, "agree_privacy": True},
        )

        assert response.status_code >= 500
        assert db_session.query(User).count() == 0


class TestBootstrapRequestBodyIdentityRemoved:
    """CLIAR-105: request body로 identity를 넘기는 구조를 제거했는지 확인."""

    def test_user_id_in_body_is_rejected(self, client, db_session, monkeypatch):
        """user_id 필드는 스키마에서 제거되었으므로, 요청에 포함되면
        extra="forbid"에 의해 422로 거부된다."""
        _seed_required_terms(db_session)
        headers = _authenticate_as(monkeypatch, sub=str(uuid.uuid4()))

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "user_id": str(uuid.uuid4()),
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 422

    def test_email_in_body_is_ignored_not_trusted(self, client, db_session, monkeypatch):
        """email 필드도 스키마에서 제거되었으므로 body에 포함하면 422로
        거부된다(클라이언트가 보낸 email이 조용히 무시되고 채택되는
        일이 없어야 한다는 요구사항을, "그 필드 자체가 존재하지 않음"
        으로 만족한다)."""
        _seed_required_terms(db_session)
        headers = _authenticate_as(
            monkeypatch, sub=str(uuid.uuid4()), email="verified@example.com"
        )

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "email": "attacker-supplied@example.com",
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 422


class TestBootstrapValidation:

    def test_duplicate_user_id_returns_409(self, client, db_session, monkeypatch):
        member_id = uuid.uuid4()
        _create_member(
            db_session,
            member_id=member_id,
            email="old@example.com",
            nickname="oldnick",
        )
        headers = _authenticate_as(monkeypatch, sub=str(member_id), email="new@example.com")

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 409

    def test_duplicate_email_returns_409(self, client, db_session, monkeypatch):
        _create_member(
            db_session,
            email="same@example.com",
            nickname="oldnick",
        )
        headers = _authenticate_as(
            monkeypatch, sub=str(uuid.uuid4()), email="same@example.com"
        )

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 409

    def test_duplicate_nickname_is_allowed(self, client, db_session, monkeypatch):
        """CLIAR-87 확정 요구사항: member.nickname은 UNIQUE 제약이 없으며
        중복을 허용한다. 다른 회원과 동일한 nickname으로도 정상 생성되어야
        한다."""
        _seed_required_terms(db_session)
        _create_member(
            db_session,
            email="old@example.com",
            nickname="same-nick",
        )
        headers = _authenticate_as(
            monkeypatch, sub=str(uuid.uuid4()), email="new@example.com"
        )

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "same-nick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 201
        assert response.json()["nickname"] == "same-nick"

    def test_required_consent_false_returns_400(self, client, monkeypatch):
        headers = _authenticate_as(monkeypatch, sub=str(uuid.uuid4()))

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "newnick",
                "agree_terms": False,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 400

    def test_blank_nickname_returns_400(self, client, monkeypatch):
        headers = _authenticate_as(monkeypatch, sub=str(uuid.uuid4()))

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "   ",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 400


class TestBootstrapMemberAgreement:
    """CLIAR-92: 회원 최초 생성 시 member_agreement AGREE 이력 저장."""

    def test_creates_agreements_for_terms_of_service_and_privacy(
        self, client, db_session, monkeypatch
    ):
        _seed_required_terms(db_session)
        member_id = str(uuid.uuid4())
        headers = _authenticate_as(monkeypatch, sub=member_id, email="test@example.com")

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 201

        member = db_session.query(User).filter(User.member_id == uuid.UUID(member_id)).one()
        agreements = (
            db_session.query(MemberAgreement)
            .filter(MemberAgreement.member_id == member.member_id)
            .all()
        )
        assert len(agreements) == 2

        codes = {
            db_session.query(Terms).filter(Terms.id == a.terms_id).one().code
            for a in agreements
        }
        assert codes == {"TERMS_OF_SERVICE", "PRIVACY"}

    def test_agree_ai_analysis_true_creates_third_agreement(
        self, client, db_session, monkeypatch
    ):
        _seed_required_terms(db_session)
        _seed_terms(db_session, "AI_ANALYSIS")
        member_id = str(uuid.uuid4())
        headers = _authenticate_as(monkeypatch, sub=member_id, email="test@example.com")

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_ai_analysis": True,
            },
        )

        assert response.status_code == 201

        member = db_session.query(User).filter(User.member_id == uuid.UUID(member_id)).one()
        agreements = (
            db_session.query(MemberAgreement)
            .filter(MemberAgreement.member_id == member.member_id)
            .all()
        )
        codes = {
            db_session.query(Terms).filter(Terms.id == a.terms_id).one().code
            for a in agreements
        }
        assert codes == {"TERMS_OF_SERVICE", "PRIVACY", "AI_ANALYSIS"}

    def test_agree_ai_analysis_false_creates_no_ai_agreement(
        self, client, db_session, monkeypatch
    ):
        _seed_required_terms(db_session)
        _seed_terms(db_session, "AI_ANALYSIS")
        member_id = str(uuid.uuid4())
        headers = _authenticate_as(monkeypatch, sub=member_id, email="test@example.com")

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "haechan",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_ai_analysis": False,
            },
        )

        assert response.status_code == 201

        member = db_session.query(User).filter(User.member_id == uuid.UUID(member_id)).one()
        agreements = (
            db_session.query(MemberAgreement)
            .filter(MemberAgreement.member_id == member.member_id)
            .all()
        )
        codes = {
            db_session.query(Terms).filter(Terms.id == a.terms_id).one().code
            for a in agreements
        }
        assert "AI_ANALYSIS" not in codes

    def test_missing_terms_of_service_returns_503_and_no_member_created(
        self, client, db_session, monkeypatch
    ):
        """필수 약관이 현재 적용 중인 상태로 없으면 서버 설정 문제로
        503을 반환하고, member row가 DB에 남지 않아야 한다."""
        _seed_terms(db_session, "PRIVACY")
        headers = _authenticate_as(monkeypatch, sub=str(uuid.uuid4()))

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
            },
        )

        assert response.status_code == 503
        assert db_session.query(User).count() == 0

    def test_agree_ai_analysis_true_but_missing_ai_terms_returns_503(
        self, client, db_session, monkeypatch
    ):
        _seed_required_terms(db_session)
        headers = _authenticate_as(monkeypatch, sub=str(uuid.uuid4()))

        response = client.post(
            ENDPOINT,
            headers=headers,
            json={
                "nickname": "newnick",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_ai_analysis": True,
            },
        )

        assert response.status_code == 503
        assert db_session.query(User).count() == 0
        assert db_session.query(MemberAgreement).count() == 0
