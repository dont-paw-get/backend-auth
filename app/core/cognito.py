from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import settings


COGNITO_ISSUER = (
    f"https://cognito-idp.{settings.AWS_REGION}.amazonaws.com/"
    f"{settings.COGNITO_USER_POOL_ID}"
)

JWKS_URL = f"{COGNITO_ISSUER}/.well-known/jwks.json"


@lru_cache()
def get_jwk_client() -> PyJWKClient:
    """
    Cognito JWKS 공개키 클라이언트.
    
    Cognito는 JWT 서명 검증을 위해 공개키(JWK)를 제공한다.
    이 키를 이용해 Access Token이 진짜 Cognito에서 발급된 것인지 검증한다.
    """
    return PyJWKClient(JWKS_URL)


def verify_cognito_token(token: str) -> dict:
    """
    Cognito Access Token 검증.

    검증:
    - JWT signature
    - issuer
    - token expiration

    성공:
    -> JWT payload 반환

    payload 예:
    {
        "sub": "cognito-user-id",
        "email": "user@test.com"
    }
    """

    try:
        signing_key = get_jwk_client().get_signing_key_from_jwt(token)

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=COGNITO_ISSUER,
            options={
                "verify_aud": False,
            },
        )

        return payload

    except Exception as e:
        raise ValueError("Invalid Cognito token") from e