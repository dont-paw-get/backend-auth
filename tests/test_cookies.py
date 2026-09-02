"""
app/core/cookies.py의 refresh 쿠키 set/clear helper 테스트
(CLIAR-148, Phase 1).

이번 티켓에서는 실제 /auth/login, /auth/refresh, /auth/logout
endpoint에 연결하지 않으므로, FastAPI Response 객체를 직접 만들어
set_cookie/delete_cookie 호출 인자를 검증하는 순수 단위 테스트만
작성한다.
"""

from fastapi import Response

from app.core import cookies
from app.core.config import settings


class TestSetRefreshCookies:
    def test_sets_refresh_token_cookie(self):
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        assert any("refresh_token=rt-value" in header for header in set_cookie_headers)

    def test_sets_refresh_sub_cookie(self):
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        assert any("refresh_sub=sub-value" in header for header in set_cookie_headers)

    def test_both_cookies_are_httponly(self):
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        assert len(set_cookie_headers) == 2
        for header in set_cookie_headers:
            assert "HttpOnly" in header

    def test_secure_reflects_settings_true(self, monkeypatch):
        monkeypatch.setattr(settings, "COOKIE_SECURE", True)
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            assert "Secure" in header

    def test_secure_reflects_settings_false(self, monkeypatch):
        monkeypatch.setattr(settings, "COOKIE_SECURE", False)
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            assert "Secure" not in header

    def test_samesite_reflects_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "COOKIE_SAMESITE", "strict")
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            assert "samesite=strict" in header.lower()

    def test_path_is_scoped_to_auth_endpoints(self):
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            assert "Path=/api/v1/auth" in header

    def test_domain_reflects_settings_when_set(self, monkeypatch):
        monkeypatch.setattr(settings, "COOKIE_DOMAIN", "example.com")
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            assert "Domain=example.com" in header

    def test_domain_omitted_when_none(self, monkeypatch):
        monkeypatch.setattr(settings, "COOKIE_DOMAIN", None)
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            assert "Domain=" not in header

    def test_max_age_set_when_positive(self, monkeypatch):
        # CLIAR-230: COOKIE_MAX_AGE가 양수면 두 쿠키가 영속 쿠키가 되도록
        # Max-Age가 실린다(브라우저 재시작 후에도 유지).
        monkeypatch.setattr(settings, "COOKIE_MAX_AGE", 2592000)
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        assert len(set_cookie_headers) == 2
        for header in set_cookie_headers:
            assert "Max-Age=2592000" in header

    def test_max_age_omitted_when_zero(self, monkeypatch):
        # 0 이하이면 Max-Age를 생략해 기존 세션 쿠키 동작을 유지한다.
        monkeypatch.setattr(settings, "COOKIE_MAX_AGE", 0)
        response = Response()

        cookies.set_refresh_cookies(response, refresh_token="rt-value", sub="sub-value")

        set_cookie_headers = response.headers.getlist("set-cookie")
        assert len(set_cookie_headers) == 2
        for header in set_cookie_headers:
            assert "Max-Age" not in header


class TestClearRefreshCookies:
    def test_clears_both_cookies(self):
        response = Response()

        cookies.clear_refresh_cookies(response)

        set_cookie_headers = response.headers.getlist("set-cookie")
        assert len(set_cookie_headers) == 2
        names = {header.split("=")[0] for header in set_cookie_headers}
        assert names == {"refresh_token", "refresh_sub"}

    def test_cleared_cookies_have_empty_value(self):
        response = Response()

        cookies.clear_refresh_cookies(response)

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            # delete_cookie는 값이 빈 문자열이고 즉시 만료되는 쿠키를 만든다.
            assert '=""' in header or header.split(";")[0].endswith("=")

    def test_cleared_cookies_use_same_path(self):
        response = Response()

        cookies.clear_refresh_cookies(response)

        set_cookie_headers = response.headers.getlist("set-cookie")
        for header in set_cookie_headers:
            assert "Path=/api/v1/auth" in header


class TestCookieValuesAreNotLogged:
    def test_module_has_no_logger_or_print_of_token_values(self):
        """cookies.py 소스 자체에 로깅 호출이 없어야 한다(토큰 값을
        로그에 남길 방법 자체가 코드에 존재하지 않아야 함)."""
        import inspect

        source = inspect.getsource(cookies)

        assert "logger" not in source
        assert "print(" not in source
