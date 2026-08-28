"""
BE 주도 인증 전환(PLAN.md)을 위한 신규 backend 전용 Cognito App
Client(secret 있음) 호출 공통 기반 (CLIAR-148, Phase 1).

기존 app/core/cognito.py는 FE App Client(secret 없음)로 발급된 Access
Token 검증/GetUser/DeleteUser를 담당하며 이번 티켓에서 변경하지
않는다. 이 모듈은 그와 별도로, 앞으로 SignUp/ConfirmSignUp/
InitiateAuth 등 "secret이 있는 App Client"로 호출할 non-admin
Cognito API들이 공통으로 필요로 하는 SECRET_HASH 계산만 우선
제공한다.

Phase 1 범위: SECRET_HASH 계산 함수만 구현한다. SignUp 등 실제 API
wrapper는 그 기능을 실제로 사용하는 Phase 3/4/5에서 추가한다(미리
만들어두고 쓰지 않는 코드를 최소화하기 위함).

Phase 3(CLIAR-151): SignUp/ConfirmSignUp/ResendConfirmationCode +
보상용 admin API wrapper 추가.
Phase 4(CLIAR-153): InitiateAuth(USER_PASSWORD_AUTH/REFRESH_TOKEN_AUTH),
GetUser(sub 조회), RevokeToken wrapper 추가.
"""

import base64
import hashlib
import hmac

from app.core.cognito import get_cognito_idp_client  # noqa: F401  (Phase 3+ wrapper가 재사용)
from app.core.config import settings


def secret_hash(username: str) -> str:
    """
    AWS Cognito SECRET_HASH를 계산한다.

    규칙(AWS 공식):
        message = username + client_id
        key     = client_secret
        digest  = HMAC-SHA256(key, message)
        result  = base64(digest)

    client_id/client_secret은 항상 settings.COGNITO_BACKEND_CLIENT_ID/
    settings.COGNITO_BACKEND_CLIENT_SECRET에서 읽으며 하드코딩하지
    않는다. 이 값들은 신규 backend 전용 App Client(secret 보유) 것이며,
    기존 FE App Client(COGNITO_CLIENT_ID, secret 없음)와는 다른 값이다.

    신규 backend App Client 설정이 아직 배포 환경에 없는 경우(Phase 1
    시점에는 정상적인 상태), 값이 비어 있으면 조용히 잘못된 해시를
    계산하지 않고 명확한 RuntimeError를 발생시킨다.
    """
    client_id = settings.COGNITO_BACKEND_CLIENT_ID
    client_secret = settings.COGNITO_BACKEND_CLIENT_SECRET

    if not client_id or not client_secret:
        raise RuntimeError(
            "COGNITO_BACKEND_CLIENT_ID/COGNITO_BACKEND_CLIENT_SECRET "
            "must be configured to compute a Cognito SECRET_HASH"
        )

    message = (username + client_id).encode("utf-8")
    key = client_secret.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()

    return base64.b64encode(digest).decode("utf-8")


# ---------------------------------------------------------------------------
# Phase 3 (CLIAR-151): 회원가입 wrapper.
#
# 이 함수들은 boto3 예외(ClientError/EndpointConnectionError)를 스스로
# 잡지 않고 그대로 호출자에게 전파한다. 매핑 로직은 오직
# app/services/signup_service.py에서 app/core/cognito_errors.py를 통해
# 한 곳에서만 결정한다(endpoint/service마다 매핑을 중복 작성하지
# 않기 위함). 이는 기존 app/core/cognito.py의 wrapper들(각자 ClientError를
# 잡아 ValueError/RuntimeError로 변환하는 방식)과는 다른 패턴인데,
# cognito_errors.py가 아직 없던 시점에 작성된 코드이기 때문이다.
#
# Username은 email을 그대로 사용한다. 이는 Cognito User Pool이 email을
# 로그인 식별자(alias 또는 실제 username)로 허용하도록 구성되어 있다는
# 전제이며(PLAN.md §8.1 "email alias가 활성화되어 있어야 한다(미설정 시
# 추가)"), 이 전제가 실제로 충족되는지는 IAM/User Pool 설정이 아직
# 완료되지 않아 코드만으로 확정할 수 없다(완료 보고 참고).
# ---------------------------------------------------------------------------


def _require_backend_client_id() -> str:
    """
    secret_hash()가 이미 COGNITO_BACKEND_CLIENT_ID/SECRET의 존재를
    검증하므로, 이 함수는 secret_hash() 호출 이후에만 사용해 중복
    검증 없이 값을 꺼낸다.
    """
    return settings.COGNITO_BACKEND_CLIENT_ID  # type: ignore[return-value]


def sign_up(*, email: str, password: str) -> dict:
    """
    신규 backend 전용 App Client로 Cognito SignUp을 호출한다.

    ClientError(예: UsernameExistsException, InvalidPasswordException)와
    EndpointConnectionError는 이 함수가 잡지 않고 그대로 전파한다.
    호출자(app/services/signup_service.py)가 UsernameExistsException을
    특별히 분기(orphan recovery)하고, 그 외에는 cognito_errors.py의
    매핑을 적용한다.

    반환값은 boto3 sign_up() 응답 원본이다(UserSub, CodeDeliveryDetails
    등 포함).
    """
    hash_value = secret_hash(email)
    client = get_cognito_idp_client()
    return client.sign_up(
        ClientId=_require_backend_client_id(),
        SecretHash=hash_value,
        Username=email,
        Password=password,
        UserAttributes=[{"Name": "email", "Value": email}],
    )


def confirm_sign_up(*, email: str, confirmation_code: str) -> None:
    """
    Cognito ConfirmSignUp을 호출해 이메일 인증 코드를 검증한다.

    ClientError(예: CodeMismatchException, ExpiredCodeException)는
    잡지 않고 그대로 전파한다.
    """
    hash_value = secret_hash(email)
    client = get_cognito_idp_client()
    client.confirm_sign_up(
        ClientId=_require_backend_client_id(),
        SecretHash=hash_value,
        Username=email,
        ConfirmationCode=confirmation_code,
    )


def resend_confirmation_code(*, email: str) -> dict:
    """
    Cognito ResendConfirmationCode를 호출해 인증 코드를 재전송한다.

    반환값은 boto3 resend_confirmation_code() 응답 원본이다
    (CodeDeliveryDetails 포함).
    """
    hash_value = secret_hash(email)
    client = get_cognito_idp_client()
    return client.resend_confirmation_code(
        ClientId=_require_backend_client_id(),
        SecretHash=hash_value,
        Username=email,
    )


def admin_get_user(*, email: str) -> dict:
    """
    AdminGetUser로 Cognito 사용자 정보(sub 포함)를 조회한다.

    UsernameExistsException 발생 시 "이 이메일이 이미 가입된 것인지,
    DB에는 없는 고아 Cognito 계정인지"를 판별하기 위한 orphan recovery
    (PLAN.md §4.1) 전용이다.

    주의: cognito-idp:AdminGetUser IAM 권한이 필요하다. CLIAR-151
    시점에는 이 권한이 아직 워크로드에 연결되지 않았으므로(완료 보고
    참고), 실제 환경에서는 AccessDeniedException 등으로 실패할 수
    있다. 이 함수는 admin API이므로 SecretHash를 보내지 않는다(IAM
    자격증명으로 인가되며 App Client secret과는 무관하다).
    """
    client = get_cognito_idp_client()
    return client.admin_get_user(
        UserPoolId=settings.COGNITO_USER_POOL_ID,
        Username=email,
    )


def admin_delete_user(*, email: str) -> None:
    """
    AdminDeleteUser로 Cognito 사용자를 강제 삭제한다.

    Cognito SignUp은 성공했지만 뒤이은 DB(member/member_agreement)
    저장이 실패했을 때, 고아 Cognito 계정을 남기지 않기 위한 보상
    삭제 전용이다(PLAN.md §4.1 "③ 실패 시 보상").

    주의: cognito-idp:AdminDeleteUser IAM 권한이 필요하다. CLIAR-151
    시점에는 이 권한이 아직 워크로드에 연결되지 않았으므로(완료 보고
    참고), 실제 환경에서는 이 호출 자체가 실패할 수 있다. 호출자는
    이 실패를 원래 signup 실패를 성공으로 위장하는 데 쓰지 않아야
    한다.
    """
    client = get_cognito_idp_client()
    client.admin_delete_user(
        UserPoolId=settings.COGNITO_USER_POOL_ID,
        Username=email,
    )


def extract_sub_from_user_attributes(response: dict) -> str:
    """
    Cognito 응답의 UserAttributes 목록에서 sub 값을 추출한다.

    AdminGetUser와 GetUser는 응답 형태가 동일하게 UserAttributes
    목록을 갖는다. app.core.cognito.get_cognito_user_email이 GetUser
    응답에서 email 속성을 찾는 것과 동일한 패턴이며, 두 API가 같은
    추출 로직을 공유하도록 이 함수 하나만 둔다. sub 속성이
    없으면(비정상 응답) ValueError를 던진다.
    """
    for attribute in response.get("UserAttributes", []):
        if attribute.get("Name") == "sub":
            value = attribute.get("Value")
            if value:
                return value

    raise ValueError("Cognito response is missing the 'sub' attribute")


def extract_sub_from_admin_get_user(response: dict) -> str:
    """
    AdminGetUser 응답에서 sub를 추출한다(Phase 3 orphan recovery).

    실제 추출은 extract_sub_from_user_attributes와 동일하다. 기존
    호출부/테스트가 사용하는 이름을 그대로 유지하기 위해 남겨둔 얇은
    별칭이다.
    """
    return extract_sub_from_user_attributes(response)


# ---------------------------------------------------------------------------
# Phase 4 (CLIAR-153): 로그인 / 토큰 갱신 / 로그아웃 wrapper.
#
# Phase 3 wrapper들과 동일한 규칙을 따른다: boto3 예외
# (ClientError/EndpointConnectionError)를 여기서 잡지 않고 그대로
# 전파하며, HTTP 매핑은 호출자(app/services/login_service.py)가
# app/core/cognito_errors.py를 통해 한 곳에서만 결정한다.
#
# 이 모듈의 모든 호출은 신규 backend 전용 App Client(secret 있음)를
# 사용한다. 기존 FE App Client(settings.COGNITO_CLIENT_ID, secret 없음)
# 를 쓰는 app/core/cognito.py의 refresh_cognito_access_token(CLIAR-125
# legacy 경로)과는 의도적으로 분리되어 있다.
#
# 토큰/비밀번호/secret 값은 이 모듈에서 로깅하지 않는다.
# ---------------------------------------------------------------------------


def _require_backend_client_credentials() -> tuple[str, str]:
    """
    Client Secret 자체를 직접 API 파라미터로 넘겨야 하는 호출
    (RevokeToken)에서 설정값을 꺼낸다.

    secret_hash()와 동일하게, 설정이 비어 있으면 조용히 잘못된 값으로
    호출하지 않고 명확한 RuntimeError로 실패시킨다.
    """
    client_id = settings.COGNITO_BACKEND_CLIENT_ID
    client_secret = settings.COGNITO_BACKEND_CLIENT_SECRET

    if not client_id or not client_secret:
        raise RuntimeError(
            "COGNITO_BACKEND_CLIENT_ID/COGNITO_BACKEND_CLIENT_SECRET "
            "must be configured to call the Cognito backend App Client"
        )

    return client_id, client_secret


def initiate_password_auth(*, email: str, password: str) -> dict:
    """
    이메일 + 비밀번호로 Cognito InitiateAuth(USER_PASSWORD_AUTH)를
    호출한다 (PLAN.md §4.2).

    SECRET_HASH의 username은 InitiateAuth에 넘기는 USERNAME과 동일한
    값(=email)이어야 한다. Cognito가 SECRET_HASH를 검증할 때
    "요청에 담긴 USERNAME + client_id"로 다시 계산해 비교하기
    때문이다.

    반환값은 boto3 initiate_auth() 응답 원본이다
    (AuthenticationResult 또는 ChallengeName 포함).
    """
    hash_value = secret_hash(email)
    client = get_cognito_idp_client()
    return client.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=_require_backend_client_id(),
        AuthParameters={
            "USERNAME": email,
            "PASSWORD": password,
            "SECRET_HASH": hash_value,
        },
    )


def refresh_auth(*, refresh_token: str, sub: str) -> dict:
    """
    Refresh Token으로 Cognito InitiateAuth(REFRESH_TOKEN_AUTH)를
    호출한다 (PLAN.md §4.3).

    SECRET_HASH의 username은 반드시 Cognito username(=sub)이어야
    한다. REFRESH_TOKEN_AUTH 요청에는 USERNAME 파라미터가 없지만
    Cognito는 내부적으로 refresh token이 가리키는 사용자의 username
    으로 SECRET_HASH를 검증한다. refresh token 자체는 opaque
    문자열이라 BE가 여기서 sub를 추출할 수 없으므로, 로그인 시점에
    내려둔 refresh_sub 쿠키 값을 호출자가 넘겨준다.

    반환값은 boto3 initiate_auth() 응답 원본이다. Refresh Token
    Rotation이 활성화된 경우 AuthenticationResult에 새 RefreshToken이
    포함될 수 있다(현재 dev App Client는 비활성).
    """
    hash_value = secret_hash(sub)
    client = get_cognito_idp_client()
    return client.initiate_auth(
        AuthFlow="REFRESH_TOKEN_AUTH",
        ClientId=_require_backend_client_id(),
        AuthParameters={
            "REFRESH_TOKEN": refresh_token,
            "SECRET_HASH": hash_value,
        },
    )


def revoke_refresh_token(*, refresh_token: str) -> None:
    """
    Cognito RevokeToken으로 refresh token을 무효화한다
    (PLAN.md §4.3, POST /auth/logout).

    RevokeToken은 SECRET_HASH가 아니라 ClientSecret 자체를 파라미터로
    받는 몇 안 되는 non-admin API다(AWS 계약). App Client에서 token
    revocation이 활성화되어 있어야 한다(PLAN.md §8.1).
    """
    client_id, client_secret = _require_backend_client_credentials()
    client = get_cognito_idp_client()
    client.revoke_token(
        Token=refresh_token,
        ClientId=client_id,
        ClientSecret=client_secret,
    )


def get_user_sub(*, access_token: str) -> str:
    """
    방금 발급받은 Access Token으로 Cognito GetUser를 호출해 sub를
    얻는다.

    로그인 응답의 Access Token/ID Token 문자열을 서명 검증 없이
    직접 파싱하지 않기 위한 경로다. 여기서 얻는 sub는 client가
    보낸 값이 아니라 Cognito API 응답에서 온 값이므로, member 조회
    키로 그대로 신뢰할 수 있다.

    기존 app.core.cognito.get_cognito_user_email이 같은 GetUser
    응답에서 email을 꺼내는 것과 동일한 패턴이며, IAM 권한을
    요구하지 않는다(access token으로만 인가된다). ClientError/
    EndpointConnectionError는 잡지 않고 그대로 전파한다.
    """
    client = get_cognito_idp_client()
    response = client.get_user(AccessToken=access_token)
    return extract_sub_from_user_attributes(response)
