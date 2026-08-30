import logging
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import settings

logger = logging.getLogger(__name__)


class CognitoUserAlreadyDeletedError(Exception):
    """
    Cognito 쪽에서 이 access token이 가리키는 사용자가 이미 User Pool에
    존재하지 않는다고 확인된 경우 발생한다(=이미 삭제 완료).

    DeleteUser는 username이 아니라 access token만으로 대상을 특정하므로,
    UserNotFoundException은 "요청한 username을 못 찾았다"가 아니라
    "이 토큰이 가리키던 사용자가 더 이상 User Pool에 없다"는 의미로만
    안전하게 해석할 수 있다. 이 경우에 한해 호출자가 멱등하게(이미
    삭제됨 = 성공) 처리할 수 있게 한다.
    """


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


def _required_client_id() -> str | None:
    """
    verify_cognito_token()이 요구하는 client_id를 계산한다
    (CLIAR-162, Phase 7 최종: backend App Client만 허용).

    최종 정책: settings.COGNITO_BACKEND_CLIENT_ID 하나만 허용한다.
    기존 FE App Client(COGNITO_CLIENT_ID)는 더 이상 허용하지 않는다 —
    Phase 7A의 과도기 dual-accept는 프론트가 Cognito/backend-auth
    인증을 전혀 연동한 적이 없음이 확인되어 이 Phase에서 종료됐다.

    COGNITO_BACKEND_CLIENT_ID가 None이거나 빈 문자열/공백뿐이면
    (배포 환경변수 누락) None을 반환한다. 호출자는 이 경우 무조건
    거부해야 한다 — "설정이 비어 있으니 아무 client_id나 통과"시키는
    인증 우회를 절대 만들지 않기 위해서다. 매 호출마다 settings에서
    다시 읽으므로(모듈 임포트 시점에 고정하지 않음), 배포 환경변수가
    바뀌면 재기동 후 즉시 반영된다.
    """
    client_id = settings.COGNITO_BACKEND_CLIENT_ID
    if client_id is None or not client_id.strip():
        return None
    return client_id


def verify_cognito_token(token: str) -> dict:
    """
    Cognito Access Token 검증.

    검증:
    - JWT signature (JWKS)
    - issuer
    - token expiration
    - token_use == "access" (ID Token 거절)
    - client_id가 신규 backend App Client(COGNITO_BACKEND_CLIENT_ID)와
      일치 (_required_client_id() 참고, CLIAR-162 Phase 7 최종 전환)

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

    required_client_id = _required_client_id()
    if required_client_id is None or payload.get("client_id") != required_client_id:
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


def delete_cognito_user(access_token: str, *, sub: str) -> None:
    """
    검증된 Cognito Access Token으로 DeleteUser를 호출해 "현재 로그인한
    사용자 본인"의 Cognito 계정을 삭제한다(self-service 삭제).

    AWS 계약(CognitoIdentityProvider.Client.delete_user 공식 문서):
    - 이 API는 signed-in user의 access token으로만 인증하며, IAM
      policy를 평가하지 않고 IAM credentials로 authorization할 수
      없다. 따라서 이 함수는 AdminDeleteUser를 쓰지 않고, IAM
      권한/IRSA/정적 AWS credential도 요구하지 않는다.
    - access token은 scope aws.cognito.signin.user.admin을 포함해야
      한다(현재 로그인 흐름에서 이 scope가 실제로 발급되는지는 코드
      레벨에서 보장할 수 없으므로, 발급되지 않은 경우 아래
      NotAuthorizedException 경로로 흡수된다. 이는 dev 통합 테스트
      항목으로 별도 확인이 필요하다).

    예외 분류(재시도/멱등성):
    - UserNotFoundException: DeleteUser는 username이 아니라 access
      token으로만 대상을 특정하므로, 이 예외는 "요청한 이름을 못
      찾았다"가 아니라 "이 토큰이 가리키던 사용자가 이미 User Pool에
      존재하지 않는다"는 뜻으로만 발생할 수 있다. 이 경우에 한해
      CognitoUserAlreadyDeletedError를 던져 호출자가 멱등하게(이미
      삭제 완료 = 성공) 처리할 수 있게 한다.
    - NotAuthorizedException: 토큰이 유효하지 않거나(만료/폐기 등)
      권한이 없다는 뜻이며, 이것만으로는 "사용자가 이미 삭제됐다"고
      단정할 수 없다(토큰 문제와 삭제 완료 상태를 구분할 수 없는
      경우). 따라서 이 경우는 ValueError로 던져 호출자가 401로
      매핑하게 한다. 모든 NotAuthorizedException을 성공으로
      간주하지 않는다.
    - 그 외 ClientError(InternalErrorException, TooManyRequestsException
      등)와 네트워크 장애는 서버/Cognito 측 문제이므로 RuntimeError로
      던져 호출자가 5xx로 매핑하게 한다.

    로그에는 sub와 실패 종류만 남기고 access token 원문이나 다른
    개인정보는 남기지 않는다.
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    client = get_cognito_idp_client()

    try:
        client.delete_user(AccessToken=access_token)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")

        if error_code == "UserNotFoundException":
            logger.info(
                "Cognito DeleteUser: user already absent from user pool (sub=%s)",
                sub,
            )
            raise CognitoUserAlreadyDeletedError(
                "Cognito user no longer exists for this access token"
            ) from e

        if error_code == "NotAuthorizedException":
            logger.warning(
                "Cognito DeleteUser: access token rejected, "
                "cannot distinguish invalid token from already-deleted user (sub=%s)",
                sub,
            )
            raise ValueError("Cognito rejected the access token") from e

        logger.error(
            "Cognito DeleteUser failed with error_code=%s (sub=%s)",
            error_code,
            sub,
        )
        raise RuntimeError("Cognito DeleteUser call failed") from e
    except EndpointConnectionError as e:
        logger.error("Cognito DeleteUser: could not reach Cognito (sub=%s)", sub)
        raise RuntimeError("Could not reach Cognito") from e