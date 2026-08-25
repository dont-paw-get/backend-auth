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
    - JWT signature (JWKS)
    - issuer
    - token expiration
    - token_use == "access" (ID Token 거절)
    - client_id가 현재 Cognito App Client(COGNITO_CLIENT_ID)와 일치

    Cognito Access Token은 ID Token과 달리 표준 "aud" claim이 아니라
    "client_id" claim으로 발급 대상 앱 클라이언트를 나타내므로,
    jwt.decode의 verify_aud 옵션이 아니라 별도로 client_id를 직접
    비교한다.

    성공:
    -> JWT payload 반환

    payload 예:
    {
        "sub": "cognito-user-id",
        "token_use": "access",
        "client_id": "..."
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

    except Exception as e:
        raise ValueError("Invalid Cognito token") from e

    if payload.get("token_use") != "access":
        raise ValueError("Only Cognito Access Tokens are accepted (token_use must be 'access')")

    if payload.get("client_id") != settings.COGNITO_CLIENT_ID:
        raise ValueError("Token was not issued for this Cognito App Client")

    if not payload.get("sub"):
        raise ValueError("Token is missing required 'sub' claim")

    return payload


@lru_cache()
def get_cognito_idp_client():
    """
    Cognito Identity Provider(GetUser 등) 호출용 boto3 client.

    Access Token 자체에는 email이 항상 포함된다고 보장되지 않으므로,
    검증된 Access Token으로 Cognito의 GetUser API를 호출해 사용자
    속성(email)을 조회한다.
    """
    import boto3

    return boto3.client("cognito-idp", region_name=settings.AWS_REGION)


def get_cognito_user_email(access_token: str) -> str:
    """
    검증된 Cognito Access Token으로 GetUser를 호출해 email 속성을 얻는다.

    client body의 email을 신뢰하지 않고, Cognito가 실제로 보관 중인
    사용자 속성만을 신뢰된 email로 사용한다. GetUser가 토큰을 거절하면
    (만료/폐기 등) ValueError를 던져 호출자가 401로 매핑할 수 있게
    하고, Cognito와의 통신 자체가 실패하면(네트워크/일시 장애)
    RuntimeError를 던져 호출자가 5xx로 매핑할 수 있게 한다.
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    client = get_cognito_idp_client()

    try:
        response = client.get_user(AccessToken=access_token)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in {
            "NotAuthorizedException",
            "UserNotFoundException",
            "InvalidParameterException",
        }:
            raise ValueError("Cognito rejected the access token") from e
        # 그 외(서비스 장애, throttling 등)는 서버 측 문제로 취급한다.
        raise RuntimeError("Cognito GetUser call failed") from e
    except EndpointConnectionError as e:
        raise RuntimeError("Could not reach Cognito") from e

    for attribute in response.get("UserAttributes", []):
        if attribute.get("Name") == "email":
            email = attribute.get("Value")
            if email:
                return email

    raise ValueError("Cognito user has no email attribute")