"""
인증 API rate limit 테스트 (CLIAR-160, Phase 6, PLAN.md §9.1).

app/core/rate_limit.py의 단위 동작(RateLimitRule 파싱, 슬라이딩
윈도우 카운팅, client 식별)과, 실제 endpoint(login/signup/
signup/resend/password/forgot/password/reset)에 부착된 rate limit
dependency의 통합 동작을 모두 검증한다.

실제 AWS/Cognito는 호출하지 않는다. 대부분의 케이스는 rate limit
dependency가 body 검증/Cognito 호출보다 먼저 카운트된다는 사실을
이용해(app/api/auth.py의 `dependencies=[Depends(rate_limit(...))]`
가 FastAPI 의존성 해석 단계에서 독립적으로 평가됨) 최소한의 payload
(빈 dict 등)로도 endpoint별 한도 초과를 검증할 수 있다. "인증
실패/성공과 무관하게 반복 요청 자체를 막아야 한다"는 요구사항은
정상적으로 매핑된(성공하는) Cognito 호출을 mock한 케이스로 별도
검증한다.

tests/conftest.py의 autouse fixture가 테스트마다 리미터 상태를
초기화하므로, 여기서는 그 초기화가 실제로 동작함을 별도로도 검증한다.
"""

import uuid

import pytest
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.core import cognito_auth
from app.core.config import settings
from app.core.rate_limit import (
    RateLimitRule,
    SlidingWindowRateLimiter,
    _client_identifier,
    rate_limit,
    reset_rate_limits,
)
from app.main import app
from app.models.user import MemberStatus, User

LOGIN_ENDPOINT = "/api/v1/auth/login"
SIGNUP_ENDPOINT = "/api/v1/auth/signup"
RESEND_ENDPOINT = "/api/v1/auth/signup/resend"
FORGOT_ENDPOINT = "/api/v1/auth/password/forgot"
RESET_ENDPOINT = "/api/v1/auth/password/reset"


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


class _RejectingLoginClient:
    """
    InitiateAuth가 항상 NotAuthorizedException으로 거절하는 fake
    Cognito client. 실제 Cognito 호출 없이 login endpoint가 정상적인
    (Cognito까지 도달하는) 요청 경로를 타면서도 401로 끝나게 만든다
    — rate limit이 "유효해 보이는 자격 증명 실패"에도 걸리는지
    검증하는 테스트 전용이다.
    """

    def initiate_auth(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "denied"}},
            "InitiateAuth",
        )


def _patch_login_reaches_cognito_and_is_rejected(monkeypatch):
    """login endpoint가 실제 Pydantic 검증을 통과해 서비스/Cognito
    호출부까지 도달하도록, backend App Client 설정과 fake Cognito
    client를 함께 준비한다(tests/test_auth_login.py와 동일한
    패턴)."""
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setattr(
        settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret"
    )
    monkeypatch.setattr(
        cognito_auth, "get_cognito_idp_client", lambda: _RejectingLoginClient()
    )


# ---------------------------------------------------------------------------
# 단위 테스트: RateLimitRule / SlidingWindowRateLimiter / _client_identifier
# ---------------------------------------------------------------------------


class TestRateLimitRule:
    def test_parses_count_and_minute_window(self):
        rule = RateLimitRule("10/minute")

        assert rule.max_requests == 10
        assert rule.window_seconds == 60

    def test_parses_second_and_hour_units(self):
        assert RateLimitRule("3/second").window_seconds == 1
        assert RateLimitRule("100/hour").window_seconds == 3600

    @pytest.mark.parametrize(
        "spec", ["", "10", "10/day", "abc/minute", "10/minute/extra", "-1/minute"]
    )
    def test_invalid_spec_raises_value_error(self, spec):
        with pytest.raises(ValueError):
            RateLimitRule(spec)


class TestSlidingWindowRateLimiter:
    def test_allows_requests_within_the_limit(self):
        limiter = SlidingWindowRateLimiter()
        rule = RateLimitRule("3/minute")

        assert limiter.allow("k", rule) is True
        assert limiter.allow("k", rule) is True
        assert limiter.allow("k", rule) is True

    def test_blocks_requests_beyond_the_limit(self):
        limiter = SlidingWindowRateLimiter()
        rule = RateLimitRule("3/minute")
        for _ in range(3):
            assert limiter.allow("k", rule) is True

        assert limiter.allow("k", rule) is False

    def test_different_keys_have_independent_counters(self):
        limiter = SlidingWindowRateLimiter()
        rule = RateLimitRule("1/minute")

        assert limiter.allow("a", rule) is True
        assert limiter.allow("b", rule) is True
        assert limiter.allow("a", rule) is False
        assert limiter.allow("b", rule) is False

    def test_reset_clears_all_counters(self):
        limiter = SlidingWindowRateLimiter()
        rule = RateLimitRule("1/minute")
        limiter.allow("k", rule)
        assert limiter.allow("k", rule) is False

        limiter.reset()

        assert limiter.allow("k", rule) is True

    def test_blocked_attempt_is_not_recorded_as_a_new_hit(self):
        """
        한도를 초과한 시도 자체가 카운터를 계속 밀어 올리지 않는지
        확인한다(초과 후에도 동일하게 계속 거부되어야 하며, 거부된
        시도가 창을 다시 연장시키면 안 된다).
        """
        limiter = SlidingWindowRateLimiter()
        rule = RateLimitRule("1/minute")
        assert limiter.allow("k", rule) is True
        for _ in range(5):
            assert limiter.allow("k", rule) is False


class TestClientIdentifier:
    def _build_client(self):
        built_app = FastAPI()

        @built_app.get("/x")
        def _endpoint(request: Request):
            return {"id": _client_identifier(request)}

        return TestClient(built_app)

    def test_uses_x_forwarded_for_rightmost_value_when_present(self):
        """
        신뢰 hop 수(_TRUSTED_PROXY_HOPS=1)만큼 오른쪽에서 센 값,
        즉 마지막 값을 사용해야 한다 — 그 값이 ALB가 실제로 연결을
        받은 client의 IP이기 때문이다(k8s/base/ingress.yaml
        target-type: ip, ALB는 항상 헤더 맨 뒤에 append).
        """
        test_client = self._build_client()

        response = test_client.get(
            "/x", headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}
        )

        assert response.json()["id"] == "10.0.0.1"

    def test_leftmost_value_is_not_trusted(self):
        """
        leftmost는 client가 요청에 직접 써서 보낼 수 있는 임의의
        값이므로 절대 그대로 신뢰하면 안 된다(CLIAR-160 보안 리뷰).
        client가 leftmost를 요청마다 바꿔도 실제로 채택되는 값(마지막
        hop)은 그대로여야 한다.
        """
        test_client = self._build_client()

        first = test_client.get(
            "/x", headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}
        ).json()["id"]
        second = test_client.get(
            "/x", headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.1"}
        ).json()["id"]

        assert first == second == "10.0.0.1"

    def test_single_value_header_is_trusted_as_is(self):
        """
        정상적인 ALB 요청은 X-Forwarded-For에 값이 하나만 있을 수
        있다(ALB 앞에 다른 프록시가 없는 경우, k8s/cluster/
        ingressclass-alb.yaml scheme: internet-facing). 이 경우
        그 하나뿐인 값이 곧 신뢰 hop(ALB)이 채운 값이다.
        """
        test_client = self._build_client()

        response = test_client.get(
            "/x", headers={"X-Forwarded-For": "203.0.113.7"}
        )

        assert response.json()["id"] == "203.0.113.7"

    def test_falls_back_to_tcp_peer_when_fewer_values_than_trusted_hops(self):
        """
        신뢰 hop 수보다 값이 적게 온 경우(예상 밖의 형태 — 빈 값만
        있는 등)는 그 헤더를 신뢰하지 않고 TCP peer로 폴백해야 한다.
        """
        test_client = self._build_client()

        response = test_client.get("/x", headers={"X-Forwarded-For": " , "})

        assert response.json()["id"] == "testclient"

    def test_falls_back_to_tcp_peer_when_header_missing(self):
        test_client = self._build_client()

        response = test_client.get("/x")

        # starlette TestClient의 기본 peer host.
        assert response.json()["id"] == "testclient"


# ---------------------------------------------------------------------------
# rate_limit() dependency factory
# ---------------------------------------------------------------------------


class TestRateLimitDependency:
    def test_allows_up_to_the_configured_limit(self):
        reset_rate_limits()
        built_app = FastAPI()

        @built_app.get(
            "/x", dependencies=[Depends(rate_limit("unit-test-endpoint", "2/minute"))]
        )
        def _endpoint():
            return {"ok": True}

        test_client = TestClient(built_app)
        assert test_client.get("/x").status_code == 200
        assert test_client.get("/x").status_code == 200
        assert test_client.get("/x").status_code == 429

    def test_different_endpoint_names_do_not_share_a_counter(self):
        reset_rate_limits()
        built_app = FastAPI()

        @built_app.get("/a", dependencies=[Depends(rate_limit("dep-a", "1/minute"))])
        def _a():
            return {}

        @built_app.get("/b", dependencies=[Depends(rate_limit("dep-b", "1/minute"))])
        def _b():
            return {}

        test_client = TestClient(built_app)
        assert test_client.get("/a").status_code == 200
        assert test_client.get("/b").status_code == 200
        assert test_client.get("/a").status_code == 429
        assert test_client.get("/b").status_code == 429


# ---------------------------------------------------------------------------
# 통합 테스트: 실제 endpoint에 부착된 rate limit
# ---------------------------------------------------------------------------


def _exhaust(client, endpoint, times, json_body=None):
    responses = []
    for _ in range(times):
        responses.append(client.post(endpoint, json=json_body if json_body is not None else {}))
    return responses


class TestLoginRateLimit:
    def test_within_limit_is_not_rate_limited(self, client):
        responses = _exhaust(client, LOGIN_ENDPOINT, settings_limit("RATE_LIMIT_LOGIN"))
        assert all(r.status_code != 429 for r in responses)

    def test_exceeding_limit_returns_429(self, client):
        limit = settings_limit("RATE_LIMIT_LOGIN")
        _exhaust(client, LOGIN_ENDPOINT, limit)

        response = client.post(LOGIN_ENDPOINT, json={})

        assert response.status_code == 429

    def test_varying_password_does_not_bypass_the_limit(self, client, monkeypatch):
        """
        password는 limiter key에 쓰이지 않으므로, 매번 다른 password
        값을 보내도 동일한 (endpoint, IP) 카운터로 집계되어야 한다.
        """
        _patch_login_reaches_cognito_and_is_rejected(monkeypatch)
        limit = settings_limit("RATE_LIMIT_LOGIN")
        for i in range(limit):
            client.post(
                LOGIN_ENDPOINT,
                json={"email": "user@example.com", "password": f"wrong-password-{i}"},
            )

        response = client.post(
            LOGIN_ENDPOINT,
            json={"email": "user@example.com", "password": "yet-another-one"},
        )

        assert response.status_code == 429


class TestSignupRateLimit:
    def test_within_limit_is_not_rate_limited(self, client):
        responses = _exhaust(client, SIGNUP_ENDPOINT, settings_limit("RATE_LIMIT_SIGNUP"))
        assert all(r.status_code != 429 for r in responses)

    def test_exceeding_limit_returns_429(self, client):
        limit = settings_limit("RATE_LIMIT_SIGNUP")
        _exhaust(client, SIGNUP_ENDPOINT, limit)

        response = client.post(SIGNUP_ENDPOINT, json={})

        assert response.status_code == 429


class TestSignupResendRateLimit:
    def test_within_limit_is_not_rate_limited(self, client):
        responses = _exhaust(client, RESEND_ENDPOINT, settings_limit("RATE_LIMIT_SIGNUP"))
        assert all(r.status_code != 429 for r in responses)

    def test_exceeding_limit_returns_429(self, client):
        limit = settings_limit("RATE_LIMIT_SIGNUP")
        _exhaust(client, RESEND_ENDPOINT, limit)

        response = client.post(RESEND_ENDPOINT, json={})

        assert response.status_code == 429

    def test_signup_and_resend_have_independent_counters(self, client):
        """signup과 signup/resend는 같은 RATE_LIMIT_SIGNUP 값을 쓰지만
        엔드포인트 이름이 다르므로 카운터는 분리되어야 한다."""
        limit = settings_limit("RATE_LIMIT_SIGNUP")
        _exhaust(client, SIGNUP_ENDPOINT, limit)
        assert client.post(SIGNUP_ENDPOINT, json={}).status_code == 429

        response = client.post(RESEND_ENDPOINT, json={})

        assert response.status_code != 429


class TestPasswordForgotRateLimit:
    def test_within_limit_is_not_rate_limited(self, client):
        responses = _exhaust(client, FORGOT_ENDPOINT, settings_limit("RATE_LIMIT_PASSWORD"))
        assert all(r.status_code != 429 for r in responses)

    def test_exceeding_limit_returns_429(self, client):
        limit = settings_limit("RATE_LIMIT_PASSWORD")
        _exhaust(client, FORGOT_ENDPOINT, limit)

        response = client.post(FORGOT_ENDPOINT, json={})

        assert response.status_code == 429


class TestPasswordResetRateLimit:
    def test_within_limit_is_not_rate_limited(self, client):
        responses = _exhaust(client, RESET_ENDPOINT, settings_limit("RATE_LIMIT_PASSWORD"))
        assert all(r.status_code != 429 for r in responses)

    def test_exceeding_limit_returns_429(self, client):
        limit = settings_limit("RATE_LIMIT_PASSWORD")
        _exhaust(client, RESET_ENDPOINT, limit)

        response = client.post(RESET_ENDPOINT, json={})

        assert response.status_code == 429

    def test_forgot_and_reset_have_independent_counters(self, client):
        limit = settings_limit("RATE_LIMIT_PASSWORD")
        _exhaust(client, FORGOT_ENDPOINT, limit)
        assert client.post(FORGOT_ENDPOINT, json={}).status_code == 429

        response = client.post(RESET_ENDPOINT, json={})

        assert response.status_code != 429


class TestRateLimitByClientIp:
    def test_different_forwarded_ips_have_independent_counters(self, client):
        """정상적인 단일-hop 요청(ALB가 채운 값 하나뿐)이라면 서로
        다른 client IP는 독립된 카운터를 가져야 한다."""
        limit = settings_limit("RATE_LIMIT_SIGNUP")
        for _ in range(limit):
            client.post(
                SIGNUP_ENDPOINT, json={}, headers={"X-Forwarded-For": "1.1.1.1"}
            )
        assert (
            client.post(
                SIGNUP_ENDPOINT, json={}, headers={"X-Forwarded-For": "1.1.1.1"}
            ).status_code
            == 429
        )

        response = client.post(
            SIGNUP_ENDPOINT, json={}, headers={"X-Forwarded-For": "2.2.2.2"}
        )

        assert response.status_code != 429

    def test_same_real_client_is_counted_together_even_with_varying_spoofed_leftmost(
        self, client
    ):
        """
        정상 동일 client(ALB 뒤에서 실제로 같은 IP로 접속) 요청이
        같은 bucket으로 집계되는지 확인한다. leftmost 값이 요청마다
        달라져도(예: 프록시 체인이 조금 다르게 기록되는 경우) ALB가
        append한 마지막 값이 동일하면 같은 client로 집계되어야 한다.
        """
        limit = settings_limit("RATE_LIMIT_SIGNUP")
        for i in range(limit):
            client.post(
                SIGNUP_ENDPOINT,
                json={},
                headers={"X-Forwarded-For": f"attacker-value-{i}, 7.7.7.7"},
            )

        response = client.post(
            SIGNUP_ENDPOINT,
            json={},
            headers={"X-Forwarded-For": "attacker-value-final, 7.7.7.7"},
        )

        assert response.status_code == 429

    def test_spoofing_the_leftmost_value_cannot_bypass_the_limit(self, client):
        """
        CLIAR-160 보안 리뷰의 핵심 시나리오: 공격자가 매 요청마다
        X-Forwarded-For의 첫 번째 값을 임의로 바꿔 보내더라도(자신의
        실제 IP인 마지막 값은 바꿀 수 없다는 전제 — ALB가 그 값을
        직접 채워 넣으므로), rate limit을 우회할 수 없어야 한다.
        """
        limit = settings_limit("RATE_LIMIT_SIGNUP")
        real_attacker_ip = "203.0.113.99"

        responses = []
        for i in range(limit + 5):
            responses.append(
                client.post(
                    SIGNUP_ENDPOINT,
                    json={},
                    headers={
                        "X-Forwarded-For": f"spoofed-{i}-{uuid.uuid4()}, {real_attacker_ip}"
                    },
                )
            )

        assert any(r.status_code == 429 for r in responses)
        blocked_count = sum(1 for r in responses if r.status_code == 429)
        assert blocked_count >= 5


class TestRateLimitDoesNotBlockUnrelatedEndpoints:
    def test_password_change_endpoint_is_unaffected_by_login_limit(self, client):
        """
        이번 티켓의 rate limit 적용 대상 목록에 없는 endpoint
        (/auth/password/change)는 login 카운터와 무관해야 한다.
        DB/Cognito를 타지 않는 401(Authorization 헤더 없음)로
        충분히 확인 가능하다 — 429가 아니라는 것만 검증하면 된다.
        """
        limit = settings_limit("RATE_LIMIT_LOGIN")
        _exhaust(client, LOGIN_ENDPOINT, limit)
        assert client.post(LOGIN_ENDPOINT, json={}).status_code == 429

        response = client.post(
            "/api/v1/auth/password/change",
            json={"current_password": "a", "new_password": "b"},
        )

        assert response.status_code == 401
        assert response.status_code != 429


class TestRateLimitSuccessfulRequestsAreAlsoCounted:
    """
    "인증 실패/성공과 무관하게 반복 요청 남용을 막을 수 있어야 함"을
    실제로 성공하는 Cognito 호출로 검증한다.
    """

    MEMBER_SUB = "11111111-2222-3333-4444-555555555555"

    class _FakeSuccessClient:
        def initiate_auth(self, **kwargs):
            return {
                "AuthenticationResult": {
                    "AccessToken": "issued-access-token",
                    "IdToken": "issued-id-token",
                    "RefreshToken": "issued-refresh-token",
                    "ExpiresIn": 86400,
                    "TokenType": "Bearer",
                }
            }

        def get_user(self, AccessToken):
            return {
                "UserAttributes": [
                    {"Name": "sub", "Value": TestRateLimitSuccessfulRequestsAreAlsoCounted.MEMBER_SUB},
                ]
            }

    def test_repeated_successful_logins_are_still_rate_limited(
        self, client, monkeypatch
    ):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool

        from app.core.database import Base, get_db

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        session = Session(bind=engine)
        session.add(
            User(
                member_id=uuid.UUID(self.MEMBER_SUB),
                email="user@example.com",
                nickname="haechan",
                status=MemberStatus.ACTIVE,
            )
        )
        session.commit()

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        monkeypatch.setattr(
            settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id"
        )
        monkeypatch.setattr(
            settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-client-secret"
        )
        monkeypatch.setattr(
            cognito_auth, "get_cognito_idp_client", lambda: self._FakeSuccessClient()
        )

        try:
            limit = settings_limit("RATE_LIMIT_LOGIN")
            body = {"email": "user@example.com", "password": "P@ssw0rd123!"}
            for _ in range(limit):
                response = client.post(LOGIN_ENDPOINT, json=body)
                assert response.status_code == 200

            blocked = client.post(LOGIN_ENDPOINT, json=body)
            assert blocked.status_code == 429
        finally:
            app.dependency_overrides.pop(get_db, None)
            session.close()
            Base.metadata.drop_all(bind=engine)
            engine.dispose()


class TestRateLimitResetFixtureWorks:
    def test_reset_rate_limits_actually_clears_state(self, client):
        """tests/conftest.py의 autouse fixture가 의존하는 함수 자체가
        실제로 상태를 지우는지 직접 검증한다."""
        limit = settings_limit("RATE_LIMIT_SIGNUP")
        _exhaust(client, SIGNUP_ENDPOINT, limit)
        assert client.post(SIGNUP_ENDPOINT, json={}).status_code == 429

        reset_rate_limits()

        assert client.post(SIGNUP_ENDPOINT, json={}).status_code != 429


class TestRateLimitResponseDoesNotLeakSensitiveInfo:
    def test_429_body_does_not_echo_request_payload(self, client, monkeypatch):
        _patch_login_reaches_cognito_and_is_rejected(monkeypatch)
        limit = settings_limit("RATE_LIMIT_LOGIN")
        secret_password = "super-secret-password-abc123"
        for _ in range(limit):
            client.post(
                LOGIN_ENDPOINT,
                json={"email": "user@example.com", "password": secret_password},
            )

        response = client.post(
            LOGIN_ENDPOINT,
            json={"email": "user@example.com", "password": secret_password},
        )

        assert response.status_code == 429
        assert secret_password not in response.text


def settings_limit(name: str) -> int:
    """RateLimitRule 파싱 결과에서 max_requests만 꺼낸다(테스트가
    "10/minute" 같은 문자열을 직접 파싱하지 않도록)."""
    return RateLimitRule(getattr(settings, name)).max_requests
