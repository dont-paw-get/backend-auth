from fastapi import Header, HTTPException, status

from app.core.cognito import verify_cognito_token


def get_current_user_id(
    authorization: str | None = Header(default=None)
) -> str:
    """
    Authorization Header의 Cognito Access Token에서
    Cognito sub를 반환한다.
    """

    # 테스트에서 직접 함수 호출하는 경우
    # Header 객체가 들어올 수 있음
    if not isinstance(authorization, str):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Authentication integration is not configured yet",
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

    return user_id