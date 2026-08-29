"""
POST /api/v1/auth/signup, /signup/confirm, /signup/resend 테스트
(CLIAR-151, Phase 3).

BE 주도 회원가입: Cognito SignUp/ConfirmSignUp/ResendConfirmationCode를
backend-auth가 호출하고, member row(PENDING -> ACTIVE)를 저장한다.
실제 AWS/Cognito에는 접속하지 않는다. app.core.cognito_auth의
get_cognito_idp_client를 monkeypatch해 boto3 client를 대체한다
(기존 tests/test_cognito.py와 동일한 패턴).

이 endpoint들은 Bearer Access Token 인증을 요구하지 않는다(아직
로그인하지 않은 사용자가 호출하는 API이기 때문).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core import cognito_auth
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.member_agreement import MemberAgreement
from app.models.terms import Terms
from app.models.user import MemberStatus, User

SIGNUP_ENDPOINT = "/api/v1/auth/signup"
CONFIRM_ENDPOINT = "/api/v1/auth/signup/confirm"
RESEND_ENDPOINT = "/api/v1/auth/signup/resend"


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


@pytest.fixture(autouse=True)
def backend_client_settings(monkeypatch):
    """신규 backend App Client 설정이 항상 존재하는 상태를 기본값으로
    한다(SECRET_HASH 계산이 실패하지 않도록)."""
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret")
    monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", "test-user-pool-id")


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
    _seed_terms(db_session, "TERMS_OF_SERVICE")
    _seed_terms(db_session, "PRIVACY")


def _create_member(
    db_session,
    member_id=None,
    email="existing@example.com",
    nickname="existing-nick",
    status=MemberStatus.ACTIVE,
):
    member = User(
        member_id=member_id or uuid.uuid4(),
        email=email,
        nickname=nickname,
        status=status,
    )
    db_session.add(member)
    db_session.commit()
    return member


def _valid_signup_body(**overrides):
    body = {
        "email": "newuser@example.com",
        "password": "P@ssw0rd123!",
        "nickname": "haechan",
        "birth_date": "2000-01-01",
        "gender": "MALE",
        "agree_terms": True,
        "agree_privacy": True,
        "agree_ai_analysis": False,
    }
    body.update(overrides)
    return body


class _FakeCognitoClient:
    """
    signup/confirm/resend/admin API를 흉내내는 in-memory fake Cognito
    client. 테스트별로 필요한 만큼만 override해서 사용한다.
    """

    def __init__(self, *, user_sub=None):
        self.user_sub = user_sub or str(uuid.uuid4())
        self.sign_up_calls = []
        self.confirm_calls = []
        self.resend_calls = []
        self.admin_delete_calls = []

    def sign_up(self, **kwargs):
        self.sign_up_calls.append(kwargs)
        return {
            "UserSub": self.user_sub,
            "UserConfirmed": False,
            "CodeDeliveryDetails": {"Destination": "n***@example.com", "DeliveryMedium": "EMAIL"},
        }

    def confirm_sign_up(self, **kwargs):
        self.confirm_calls.append(kwargs)

    def resend_confirmation_code(self, **kwargs):
        self.resend_calls.append(kwargs)
        return {"CodeDeliveryDetails": {"Destination": "n***@example.com"}}

    def admin_delete_user(self, **kwargs):
        self.admin_delete_calls.append(kwargs)


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(cognito_auth, "get_cognito_idp_client", lambda: fake_client)


class TestSignupSuccess:
    def test_returns_201(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 201

    def test_response_status_is_pending(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.json()["status"] == "PENDING"

    def test_member_id_equals_cognito_user_sub(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        sub = str(uuid.uuid4())
        _patch_client(monkeypatch, _FakeCognitoClient(user_sub=sub))

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.json()["member_id"] == sub

    def test_member_row_created_with_pending_status(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        sub = str(uuid.uuid4())
        _patch_client(monkeypatch, _FakeCognitoClient(user_sub=sub))

        client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        member = (
            db_session.query(User).filter(User.member_id == uuid.UUID(sub)).one()
        )
        assert member.status == MemberStatus.PENDING
        assert member.email == "newuser@example.com"

    def test_member_agreement_rows_created(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        sub = str(uuid.uuid4())
        _patch_client(monkeypatch, _FakeCognitoClient(user_sub=sub))

        client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        member = (
            db_session.query(User).filter(User.member_id == uuid.UUID(sub)).one()
        )
        agreements = (
            db_session.query(MemberAgreement)
            .filter(MemberAgreement.member_id == member.member_id)
            .all()
        )
        assert len(agreements) == 2

    def test_cognito_sign_up_called_with_email_username(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        fake_client = _FakeCognitoClient()
        _patch_client(monkeypatch, fake_client)

        client.post(SIGNUP_ENDPOINT, json=_valid_signup_body(email="specific@example.com"))

        assert fake_client.sign_up_calls[0]["Username"] == "specific@example.com"


class TestSignupNicknamePolicy:
    """CLIAR-144 최종 정책: nickname 중복 허용, availability 검사 없음."""

    def test_duplicate_nickname_is_allowed(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        _create_member(db_session, email="other@example.com", nickname="haechan")
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(
            SIGNUP_ENDPOINT, json=_valid_signup_body(nickname="haechan")
        )

        assert response.status_code == 201

    def test_signup_does_not_call_exists_by_nickname(self, client, db_session, monkeypatch):
        """signup 흐름이 nickname availability를 조회하지 않는지
        확인한다(호출되면 AssertionError로 실패)."""
        from app.repositories.user_repository import UserRepository

        _seed_required_terms(db_session)
        _patch_client(monkeypatch, _FakeCognitoClient())

        def _fail_if_called(self, nickname):
            raise AssertionError("exists_by_nickname must not be called during signup")

        monkeypatch.setattr(UserRepository, "exists_by_nickname", _fail_if_called)

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 201


class TestSignupEmailDuplicate:
    def test_active_email_returns_409(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        _create_member(
            db_session, email="taken@example.com", status=MemberStatus.ACTIVE
        )
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(
            SIGNUP_ENDPOINT, json=_valid_signup_body(email="taken@example.com")
        )

        assert response.status_code == 409

    def test_pending_email_returns_409(self, client, db_session, monkeypatch):
        """PENDING email도 Cognito User Pool에서 이미 점유된 상태이므로
        신규 가입을 막아야 한다(CLIAR-144 정책)."""
        _seed_required_terms(db_session)
        _create_member(
            db_session, email="pending@example.com", status=MemberStatus.PENDING
        )
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(
            SIGNUP_ENDPOINT, json=_valid_signup_body(email="pending@example.com")
        )

        assert response.status_code == 409

    def test_active_email_duplicate_does_not_call_cognito(
        self, client, db_session, monkeypatch
    ):
        _seed_required_terms(db_session)
        _create_member(
            db_session, email="taken@example.com", status=MemberStatus.ACTIVE
        )
        fake_client = _FakeCognitoClient()
        _patch_client(monkeypatch, fake_client)

        client.post(SIGNUP_ENDPOINT, json=_valid_signup_body(email="taken@example.com"))

        assert len(fake_client.sign_up_calls) == 0


class TestSignupValidation:
    def test_missing_password_returns_422(self, client, db_session):
        body = _valid_signup_body()
        del body["password"]

        response = client.post(SIGNUP_ENDPOINT, json=body)

        assert response.status_code == 422

    def test_required_consent_false_returns_400(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(
            SIGNUP_ENDPOINT, json=_valid_signup_body(agree_terms=False)
        )

        assert response.status_code == 400

    def test_missing_required_terms_returns_503(self, client, db_session, monkeypatch):
        """TERMS_OF_SERVICE/PRIVACY가 DB에 없으면 서버 설정 오류로
        503을 반환해야 한다(기존 bootstrap과 동일한 정책)."""
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 503

    def test_password_not_logged_or_echoed_in_error_response(
        self, client, db_session, monkeypatch
    ):
        _patch_client(monkeypatch, _FakeCognitoClient())
        secret_password = "SuperSecretPassword123!"

        response = client.post(
            SIGNUP_ENDPOINT, json=_valid_signup_body(password=secret_password)
        )

        assert secret_password not in response.text

    def test_does_not_require_authorization_header(self, client, db_session, monkeypatch):
        _seed_required_terms(db_session)
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 201


class TestSignupCognitoErrorMapping:
    def test_invalid_password_returns_400(self, client, db_session, monkeypatch):
        from botocore.exceptions import ClientError

        _seed_required_terms(db_session)

        class _FailingClient(_FakeCognitoClient):
            def sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "InvalidPasswordException", "Message": "weak"}},
                    "SignUp",
                )

        _patch_client(monkeypatch, _FailingClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 400

    def test_too_many_requests_returns_429(self, client, db_session, monkeypatch):
        from botocore.exceptions import ClientError

        _seed_required_terms(db_session)

        class _FailingClient(_FakeCognitoClient):
            def sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "TooManyRequestsException", "Message": "slow down"}},
                    "SignUp",
                )

        _patch_client(monkeypatch, _FailingClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 429

    def test_endpoint_connection_error_returns_502(self, client, db_session, monkeypatch):
        from botocore.exceptions import EndpointConnectionError

        _seed_required_terms(db_session)

        class _FailingClient(_FakeCognitoClient):
            def sign_up(self, **kwargs):
                raise EndpointConnectionError(endpoint_url="https://cognito-idp.example.com")

        _patch_client(monkeypatch, _FailingClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 502


class TestSignupDbFailureCompensation:
    def test_db_failure_after_cognito_success_triggers_admin_delete_user(
        self, client, db_session, monkeypatch
    ):
        """Cognito SignUp 성공 -> DB 저장 실패 -> AdminDeleteUser 보상
        호출을 확인한다. 필수 약관을 일부러 시딩하지 않아 DB 단계에서
        RequiredTermsNotConfiguredError가 발생하도록 유도한다."""
        fake_client = _FakeCognitoClient()
        _patch_client(monkeypatch, fake_client)

        # 필수 약관을 시딩하지 않음 -> _create_pending_member 내부에서 실패
        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        assert response.status_code == 503
        assert len(fake_client.admin_delete_calls) == 1

    def test_admin_delete_user_failure_does_not_mask_original_error(
        self, client, db_session, monkeypatch
    ):
        """AdminDeleteUser 보상 자체가 실패해도, 원래 DB 실패를
        성공으로 위장하지 않아야 한다."""

        class _CompensationFailingClient(_FakeCognitoClient):
            def admin_delete_user(self, **kwargs):
                raise RuntimeError("AdminDeleteUser IAM permission denied")

        _patch_client(monkeypatch, _CompensationFailingClient())

        response = client.post(SIGNUP_ENDPOINT, json=_valid_signup_body())

        # 필수 약관이 없어 DB 저장이 실패하는 시나리오이므로, 보상
        # 삭제 실패와 무관하게 여전히 503(서버 설정 오류)이어야 한다.
        assert response.status_code == 503


class TestSignupUsernameExistsOrphanRecovery:
    def test_existing_active_member_returns_409(self, client, db_session, monkeypatch):
        """UsernameExistsException + DB에 ACTIVE member 존재 -> 409."""
        from botocore.exceptions import ClientError

        existing_sub = uuid.uuid4()
        _create_member(
            db_session,
            member_id=existing_sub,
            email="registered@example.com",
            status=MemberStatus.ACTIVE,
        )

        class _UsernameExistsClient(_FakeCognitoClient):
            def sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "UsernameExistsException", "Message": "exists"}},
                    "SignUp",
                )

            def admin_get_user(self, **kwargs):
                return {
                    "UserAttributes": [
                        {"Name": "sub", "Value": str(existing_sub)},
                        {"Name": "email", "Value": "registered@example.com"},
                    ]
                }

        _patch_client(monkeypatch, _UsernameExistsClient())

        response = client.post(
            SIGNUP_ENDPOINT, json=_valid_signup_body(email="registered@example.com")
        )

        assert response.status_code == 409

    def test_orphan_cognito_account_creates_pending_member_and_resends_code(
        self, client, db_session, monkeypatch
    ):
        """
        UsernameExistsException + Cognito에는 사용자 존재 + DB에는
        member 없음(고아) -> AdminGetUser로 sub 확보 -> PENDING member
        생성 -> ResendConfirmationCode -> 201.
        """
        from botocore.exceptions import ClientError

        _seed_required_terms(db_session)
        orphan_sub = uuid.uuid4()

        class _OrphanClient(_FakeCognitoClient):
            def sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "UsernameExistsException", "Message": "exists"}},
                    "SignUp",
                )

            def admin_get_user(self, **kwargs):
                return {
                    "UserAttributes": [
                        {"Name": "sub", "Value": str(orphan_sub)},
                        {"Name": "email", "Value": "orphan@example.com"},
                    ]
                }

        fake_client = _OrphanClient()
        _patch_client(monkeypatch, fake_client)

        response = client.post(
            SIGNUP_ENDPOINT, json=_valid_signup_body(email="orphan@example.com")
        )

        assert response.status_code == 201
        assert response.json()["member_id"] == str(orphan_sub)
        assert response.json()["status"] == "PENDING"

        member = (
            db_session.query(User).filter(User.member_id == orphan_sub).one()
        )
        assert member.status == MemberStatus.PENDING
        assert len(fake_client.resend_calls) == 1


class TestConfirmSuccess:
    def test_returns_200(self, client, db_session, monkeypatch):
        member_id = uuid.uuid4()
        _create_member(
            db_session,
            member_id=member_id,
            email="pending@example.com",
            status=MemberStatus.PENDING,
        )
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(
            CONFIRM_ENDPOINT, json={"email": "pending@example.com", "code": "123456"}
        )

        assert response.status_code == 200

    def test_status_becomes_active(self, client, db_session, monkeypatch):
        member_id = uuid.uuid4()
        _create_member(
            db_session,
            member_id=member_id,
            email="pending@example.com",
            status=MemberStatus.PENDING,
        )
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(
            CONFIRM_ENDPOINT, json={"email": "pending@example.com", "code": "123456"}
        )

        assert response.json()["status"] == "ACTIVE"
        db_session.expire_all()
        member = db_session.query(User).filter(User.member_id == member_id).one()
        assert member.status == MemberStatus.ACTIVE

    def test_calls_cognito_confirm_sign_up_with_code(self, client, db_session, monkeypatch):
        _create_member(
            db_session, email="pending@example.com", status=MemberStatus.PENDING
        )
        fake_client = _FakeCognitoClient()
        _patch_client(monkeypatch, fake_client)

        client.post(
            CONFIRM_ENDPOINT, json={"email": "pending@example.com", "code": "654321"}
        )

        assert fake_client.confirm_calls[0]["ConfirmationCode"] == "654321"


class TestConfirmErrors:
    def test_wrong_code_returns_400(self, client, db_session, monkeypatch):
        from botocore.exceptions import ClientError

        _create_member(
            db_session, email="pending@example.com", status=MemberStatus.PENDING
        )

        class _FailingClient(_FakeCognitoClient):
            def confirm_sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "CodeMismatchException", "Message": "bad code"}},
                    "ConfirmSignUp",
                )

        _patch_client(monkeypatch, _FailingClient())

        response = client.post(
            CONFIRM_ENDPOINT, json={"email": "pending@example.com", "code": "000000"}
        )

        assert response.status_code == 400

    def test_expired_code_returns_400(self, client, db_session, monkeypatch):
        from botocore.exceptions import ClientError

        _create_member(
            db_session, email="pending@example.com", status=MemberStatus.PENDING
        )

        class _FailingClient(_FakeCognitoClient):
            def confirm_sign_up(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "ExpiredCodeException", "Message": "expired"}},
                    "ConfirmSignUp",
                )

        _patch_client(monkeypatch, _FailingClient())

        response = client.post(
            CONFIRM_ENDPOINT, json={"email": "pending@example.com", "code": "111111"}
        )

        assert response.status_code == 400

    def test_member_not_found_returns_404(self, client, db_session, monkeypatch):
        """Cognito는 confirm에 성공했지만 DB에 해당 email의 member가
        없는 경우(예: signup 단계 실패 후 Cognito만 남은 비정상 상태)."""
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(
            CONFIRM_ENDPOINT, json={"email": "nomember@example.com", "code": "123456"}
        )

        assert response.status_code == 404

    def test_db_update_failure_does_not_return_200(self, client, db_session, monkeypatch):
        """Cognito ConfirmSignUp 성공 후 DB UPDATE가 실패하면 500이어야
        하며, ACTIVE로 거짓 응답(200)을 하면 안 된다."""
        _create_member(
            db_session, email="pending@example.com", status=MemberStatus.PENDING
        )
        _patch_client(monkeypatch, _FakeCognitoClient())

        # db_session.commit을 직접 패치해서 UPDATE 커밋 시점에 실패를
        # 유도한다.
        monkeypatch.setattr(db_session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        response = client.post(
            CONFIRM_ENDPOINT, json={"email": "pending@example.com", "code": "123456"}
        )

        assert response.status_code == 500
        assert response.status_code != 200


class TestResendSuccess:
    def test_returns_204(self, client, monkeypatch):
        _patch_client(monkeypatch, _FakeCognitoClient())

        response = client.post(RESEND_ENDPOINT, json={"email": "pending@example.com"})

        assert response.status_code == 204

    def test_calls_cognito_resend_confirmation_code(self, client, monkeypatch):
        fake_client = _FakeCognitoClient()
        _patch_client(monkeypatch, fake_client)

        client.post(RESEND_ENDPOINT, json={"email": "pending@example.com"})

        assert fake_client.resend_calls[0]["Username"] == "pending@example.com"


class TestResendErrors:
    def test_rate_limit_returns_429(self, client, monkeypatch):
        from botocore.exceptions import ClientError

        class _FailingClient(_FakeCognitoClient):
            def resend_confirmation_code(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "LimitExceededException", "Message": "slow down"}},
                    "ResendConfirmationCode",
                )

        _patch_client(monkeypatch, _FailingClient())

        response = client.post(RESEND_ENDPOINT, json={"email": "pending@example.com"})

        assert response.status_code == 429
