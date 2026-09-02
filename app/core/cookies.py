"""
Refresh token 관련 HttpOnly 쿠키 set/clear helper (CLIAR-148, Phase 1).

PLAN.md §4.2~4.3: 로그인 시 refresh_token과 refresh_sub 두 쿠키를
HttpOnly로 내려주고, 이후 /auth/refresh가 SECRET_HASH 계산에 필요한
username(=sub)을 그 쿠키에서 재사용한다(refresh token 자체는 opaque
문자열이라 BE가 여기서 sub를 추출할 수 없기 때문).

이번 티켓에서는 이 helper들을 실제 /auth/login, /auth/refresh,
/auth/logout endpoint에 연결하지 않는다(Phase 4에서 연결). 여기서는
FastAPI Response 객체에 대한 순수 wrapper만 제공한다.

이 모듈은 토큰 값 자체를 로그에 남기지 않는다.
"""

from fastapi import Response

from app.core.config import settings

REFRESH_TOKEN_COOKIE_NAME = "refresh_token"
REFRESH_SUB_COOKIE_NAME = "refresh_sub"

# PLAN.md §4.2: 두 쿠키 모두 /api/v1/auth 하위 endpoint(로그인/갱신/
# 로그아웃)에서만 전송되도록 Path를 한정한다. 다른 endpoint(예:
# /users/me)에는 이 쿠키가 전송되지 않는다.
COOKIE_PATH = "/api/v1/auth"


def set_refresh_cookies(response: Response, *, refresh_token: str, sub: str) -> None:
    """
    로그인/토큰 갱신 성공 시 refresh_token과 refresh_sub 쿠키를
    설정한다.

    두 쿠키 모두 HttpOnly(JS에서 접근 불가)이며, Secure/SameSite/
    Domain은 settings 값을 그대로 반영한다(하드코딩하지 않음). 이
    함수는 refresh_token/sub 값을 로그에 남기지 않는다.

    CLIAR-230: settings.COOKIE_MAX_AGE가 양수면 Max-Age를 부여해 영속
    쿠키로 만든다(브라우저 재시작 후에도 유지). access token은 프론트
    메모리에만 있으므로, 세션 쿠키로 두면 브라우저/탭을 닫는 순간
    refresh 쿠키가 사라져 재방문 시 /auth/refresh가 "쿠키 누락"으로
    401이 된다. 0 이하이면 Max-Age를 생략해 기존 세션 쿠키로 둔다.
    """
    cookie_kwargs = dict(
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path=COOKIE_PATH,
        domain=settings.COOKIE_DOMAIN,
    )

    # Max-Age는 양수일 때만 부여한다. 0 이하이거나 미설정이면 Starlette
    # 기본 동작(Max-Age/Expires 없는 세션 쿠키)을 그대로 유지한다.
    if settings.COOKIE_MAX_AGE > 0:
        cookie_kwargs["max_age"] = settings.COOKIE_MAX_AGE

    response.set_cookie(REFRESH_TOKEN_COOKIE_NAME, refresh_token, **cookie_kwargs)
    response.set_cookie(REFRESH_SUB_COOKIE_NAME, sub, **cookie_kwargs)


def clear_refresh_cookies(response: Response) -> None:
    """
    로그아웃 또는 refresh token이 더 이상 유효하지 않을 때 두 쿠키를
    모두 제거한다.

    delete_cookie는 set_cookie와 동일한 path/domain/samesite/secure
    속성으로 호출해야 브라우저가 실제로 쿠키를 지운다.
    """
    delete_kwargs = dict(
        path=COOKIE_PATH,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
    )

    response.delete_cookie(REFRESH_TOKEN_COOKIE_NAME, **delete_kwargs)
    response.delete_cookie(REFRESH_SUB_COOKIE_NAME, **delete_kwargs)
