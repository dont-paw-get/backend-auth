"""
CORS 설정 테스트 (CLIAR-153, Phase 4, PLAN.md §8.4).

refresh_token/refresh_sub는 HttpOnly 쿠키로 전달되므로 FE가 다른
origin에서 backend-auth를 호출하려면 allow_credentials=True가
필수다. 브라우저는 allow_credentials=True와 와일드카드 origin을 함께
허용하지 않으므로, 허용 origin은 CORS_ALLOWED_ORIGINS에서 명시적으로
받아야 한다.

app.main.app은 import 시점에 미들웨어가 등록되므로, origin 설정을
바꿔가며 검증하는 테스트는 configure_cors()로 별도 FastAPI 앱을
구성해서 확인한다.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Settings, settings
from app.main import app as main_app
from app.main import configure_cors


def _build_app():
    app = FastAPI()
    configure_cors(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return app


def _cors_middleware(app):
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return middleware
    return None


class TestCorsAllowedOriginsParsing:
    def test_empty_setting_yields_no_origins(self):
        assert Settings(
            DATABASE_URL="x",
            AWS_REGION="x",
            COGNITO_USER_POOL_ID="x",
            CORS_ALLOWED_ORIGINS="",
        ).cors_allowed_origins_list == []

    def test_csv_is_split_and_stripped(self):
        parsed = Settings(
            DATABASE_URL="x",
            AWS_REGION="x",
            COGNITO_USER_POOL_ID="x",
            CORS_ALLOWED_ORIGINS="https://a.example.com, https://b.example.com",
        ).cors_allowed_origins_list

        assert parsed == ["https://a.example.com", "https://b.example.com"]

    def test_wildcard_origin_is_dropped(self):
        """allow_credentials=True와 "*" 조합은 브라우저가 허용하지
        않으므로, 설정에 들어오더라도 그 조합을 만들지 않는다."""
        parsed = Settings(
            DATABASE_URL="x",
            AWS_REGION="x",
            COGNITO_USER_POOL_ID="x",
            CORS_ALLOWED_ORIGINS="*, https://a.example.com",
        ).cors_allowed_origins_list

        assert parsed == ["https://a.example.com"]


class TestCorsMiddlewareConfiguration:
    def test_main_app_registers_cors_middleware(self):
        assert _cors_middleware(main_app) is not None

    def test_allow_credentials_is_true(self):
        middleware = _cors_middleware(main_app)

        assert middleware.kwargs["allow_credentials"] is True

    def test_wildcard_origin_is_never_configured(self):
        middleware = _cors_middleware(main_app)

        assert "*" not in middleware.kwargs["allow_origins"]


class TestCorsBehaviour:
    def test_configured_origin_is_allowed_with_credentials(self, monkeypatch):
        monkeypatch.setattr(
            settings, "CORS_ALLOWED_ORIGINS", "https://app.example.com"
        )
        client = TestClient(_build_app())

        response = client.get("/ping", headers={"Origin": "https://app.example.com"})

        assert response.headers["access-control-allow-origin"] == (
            "https://app.example.com"
        )
        assert response.headers["access-control-allow-credentials"] == "true"

    def test_unlisted_origin_is_not_allowed(self, monkeypatch):
        monkeypatch.setattr(
            settings, "CORS_ALLOWED_ORIGINS", "https://app.example.com"
        )
        client = TestClient(_build_app())

        response = client.get("/ping", headers={"Origin": "https://evil.example.com"})

        assert "access-control-allow-origin" not in response.headers

    def test_preflight_allows_post_with_credentials(self, monkeypatch):
        monkeypatch.setattr(
            settings, "CORS_ALLOWED_ORIGINS", "https://app.example.com"
        )
        client = TestClient(_build_app())

        response = client.options(
            "/ping",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-credentials"] == "true"
        assert "POST" in response.headers["access-control-allow-methods"]

    def test_unconfigured_origins_allow_nothing(self, monkeypatch):
        """CORS_ALLOWED_ORIGINS가 아직 배포되지 않은 환경에서도
        startup이 깨지지 않고, cross-origin은 그냥 허용되지 않는다."""
        monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", "")
        client = TestClient(_build_app())

        response = client.get("/ping", headers={"Origin": "https://app.example.com"})

        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    def test_same_origin_requests_are_unaffected(self, monkeypatch):
        monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", "")
        client = TestClient(_build_app())

        assert client.get("/ping").status_code == 200


class TestExistingRoutesStillRegistered:
    """CORS 미들웨어 추가로 기존 라우트가 사라지지 않았는지 확인한다."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/auth/availability",
            "/api/v1/auth/signup",
            "/api/v1/auth/signup/confirm",
            "/api/v1/auth/signup/resend",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
            "/api/v1/users/me",
        ],
    )
    def test_route_is_present_in_openapi(self, path):
        assert path in main_app.openapi()["paths"]
