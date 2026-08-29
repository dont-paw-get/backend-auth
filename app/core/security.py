from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer

from app.core.cognito import verify_cognito_token

# Swagger UI의 상단 "Authorize" 버튼에 Bearer Access Token 입력창을
# 노출하기 위한 security scheme. 실제 인증 로직은 여전히
# get_current_user_id/get_current_access_token(Authorization 헤더 직접
# 파싱)이 담당하며, 이 scheme은 OpenAPI 문서화 목적으로만 endpoint의
# dependency에 추가한다. Swagger에서 Authorize로 입력한 토큰은 실제
# Authorization: Bearer <token> 헤더로 전송되므로 기존 파싱 로직과
# 자연스럽게 맞물린다. auto_error=False로 두어, 헤더가 없을 때의 401
# 응답 정책은 여전히 get_current_user_id 쪽에서 결정한다.
bearer_scheme = HTTPBearer(auto_error=False)


def _extract_and_verify_bearer_token(
    authorization: str | None = Header(default=None, include_in_schema=False),
) -> tuple[str, dict]:
    """
    Authorization 헤더에서 Cognito Access Token을 추출하고 검증한다.

    GET/PATCH/DELETE /users/me, POST /auth/password/change가 모두
    동일한 검증 경로를 공유하도록, Bearer 파싱과 Cognito 검증 로직을
    이 한 곳에 모아두고 get_current_user_id/get_current_access_token이
    이를 재사용한다(인증 로직을 endpoint/함수마다 복붙하지 않는다).

    이 함수 자체를 FastAPI dependency로 선언해두면(파라미터에
    Header(...)를 직접 받는 형태), get_current_user_id와
    get_current_access_token이 "동일한 callable"을 Depends()하는
    한 같은 HTTP request 안에서 FastAPI의 dependency 캐시에 의해
    실제 검증(JWKS 서명 확인 등)은 한 번만 실행된다.
    """

    # 테스트에서 직접 함수 호출하는 경우
    # Header 객체가 들어올 수 있음
    if not isinstance(authorization, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
        )

    token = authorization.replace("Bearer ", "", 1)

    try:
        payload = verify_cognito_token(token)

    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Cognito token",
        )

    user_id = payload.get("sub")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Cognito sub",
        )

    return token, payload


def get_current_user_id(
    verified: tuple[str, dict] = Depends(_extract_and_verify_bearer_token),
) -> str:
    """
    Authorization Header의 Cognito Access Token에서
    Cognito sub를 반환한다.
    """
    # 테스트 등에서 dependency 주입 없이 이 함수를 직접 호출하는 경우
    # verified가 실제 값이 아니라 FastAPI의 Depends(...) 마커 객체
    # 그대로 남는다. CLIAR-105에서 Cognito 인증 연동이 실제로
    # 구현되었으므로, 이 경우도 "인증 연동 미구성"이 아니라 일반적인
    # 인증 실패(401)로 취급한다.
    if not isinstance(verified, tuple):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
        )
    _, payload = verified
    return payload["sub"]


def get_current_access_token(
    verified: tuple[str, dict] = Depends(_extract_and_verify_bearer_token),
) -> str:
    """
    Authorization Header에서 검증된 Cognito Access Token 원문을 반환한다.

    DELETE /users/me(Cognito DeleteUser 호출)나 POST
    /auth/password/change(Cognito ChangePassword 호출)처럼 sub 외에
    access token 원문 자체가 추가로 필요한 endpoint에서 사용한다.
    get_current_user_id와 동일한 검증 경로
    (_extract_and_verify_bearer_token)를 공유하며, 같은 request
    안에서 이 endpoint가 get_current_user_id도 함께 Depends()하면
    FastAPI dependency 캐시로 실제 검증은 한 번만 실행된다.
    """
    token, _ = verified
    return token