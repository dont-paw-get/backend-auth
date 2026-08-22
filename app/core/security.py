"""
인증된 사용자(Cognito sub) 식별 기반.

최종 아키텍처:
    React -> Amazon Cognito Managed Login -> Access Token
    -> API Gateway (JWT 검증) -> backend-auth -> Cognito sub 기준 MEMBER 조회

backend-auth는 회원가입/로그인/JWT 발급을 직접 구현하지 않는다.
Access Token(JWT) 검증은 API Gateway가 담당하고, backend-auth는
"이미 인증이 끝난 요청"에서 Cognito sub 값만 꺼내 쓰면 된다.

하지만 다음이 아직 결정되지 않았다:
    - API Gateway가 아직 구축되지 않음
    - JWT authorizer 이후 sub를 backend에 전달하는 구체적인 방식이 확정되지 않음
      (커스텀 헤더인지, request context 주입인지 등)
    - 특정 헤더를 Gateway가 항상 채워 넣는다는 보장도 없음

따라서 이 시점에서 특정 HTTP 헤더 값을 인증된 사용자로 신뢰하는 것은
보안상 위험하다 (클라이언트가 그 헤더를 임의로 조작해 다른 사용자를
사칭할 수 있음). 이 함수는 실제 연동 방식이 확정되기 전까지
"인증 연동이 아직 구성되지 않았음"을 명확히 실패시키는 안전한
placeholder이며, 다른 코드(app/api/deps.py 등)는 이 함수의 시그니처만
의존하므로 실제 Gateway 연동이 확정되면 이 함수 내부만 교체하면 된다.

테스트에서는 FastAPI의 `app.dependency_overrides[get_current_user_id]`를
이용해 임의의 사용자 ID(Cognito sub 역할)를 주입해서 검증한다.
"""

from fastapi import HTTPException, status


def get_current_user_id() -> str:
    """
    현재 요청을 보낸 사용자의 Cognito sub(=MEMBER.user_id)를 반환한다.

    실제 API Gateway/Cognito 연동 방식이 아직 확정되지 않았으므로,
    이 함수는 어떤 HTTP 헤더도 인증 결과로 신뢰하지 않는다.
    실제 요청에서 호출되면 항상 501을 반환해 "인증 연동이 아직
    구성되지 않았음"을 명확히 알린다.

    실제 연동이 확정되면 이 함수 내부를 교체해 JWT claim이나
    Gateway가 전달하는 컨텍스트에서 sub를 꺼내도록 구현한다.
    호출하는 쪽(app/api/deps.py 등)은 수정할 필요가 없다.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Authentication integration is not configured yet",
    )
