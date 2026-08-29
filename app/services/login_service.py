"""
BE 주도 로그인 / 토큰 갱신 / 로그아웃 오케스트레이션
(CLIAR-153, Phase 4, PLAN.md §4.2~4.3).

app/api/auth.py의 login/refresh/logout endpoint는 request parsing,
쿠키 read/set/clear, 그리고 예외 -> HTTPException 변환만 담당한다.
Cognito 호출 + member 상태 판정이 뒤섞인 흐름은 모두 여기에 둔다
(app/services/signup_service.py와 동일한 책임 분리).

이 모듈은 신규 backend App Client(secret 있음) 전용이다. CLIAR-125의
legacy body 기반 refresh(기존 FE App Client, SECRET_HASH 없음)와
그 전용 함수(app/services/auth_service.py의 refresh_access_token())는
CLIAR-162 Phase 7에서 파일 단위로 완전히 제거되었다.

이 모듈은 password / access token / id token / refresh token /
client secret / Cognito sub 값을 로그에 남기지 않는다(PLAN.md §9.3).
"""

import logging
import uuid
from dataclasses import dataclass

from app.core.cognito_auth import (
    get_user_sub,
    initiate_password_auth,
    refresh_auth,
    revoke_refresh_token,
)
from app.core.cognito_errors import (
    DEFAULT_CLIENT_ERROR,
    CognitoApiError,
    cognito_client_error_to_exception,
    connection_error_to_exception,
)
from app.models.user import MemberStatus, User
from app.repositories.user_repository import UserRepository
from app.services.member_service import _normalize_email

logger = logging.getLogger(__name__)


class LoginError(Exception):
    """로그인 오케스트레이션 관련 도메인 오류의 공통 베이스."""


class MemberNotFoundError(LoginError):
    """Cognito 인증은 성공했지만 그 sub에 해당하는 member row가 없는 경우."""


class MemberEmailNotVerifiedError(LoginError):
    """member.status가 PENDING인 경우(이메일 인증 미완료)."""


class MemberWithdrawnError(LoginError):
    """member.status가 WITHDRAWN이거나 deleted_at이 설정된 경우."""


class InvalidCognitoIdentityError(LoginError):
    """
    Cognito가 반환한 sub가 UUID가 아니어서 member_id로 조회할 수 없는
    경우.

    member.member_id는 UUID 컬럼이고 Cognito sub를 그대로 저장한다
    (app/api/deps.py의 _lookup_member_by_sub와 동일한 전제). 정상적인
    Cognito User Pool에서는 발생하지 않으며, 발생했다면 사용자 입력
    문제가 아니라 서버/Cognito 구성 문제다.
    """


@dataclass(frozen=True)
class LoginResult:
    """
    로그인 성공 결과.

    refresh_token과 sub는 router가 HttpOnly 쿠키로만 내보내며 응답
    body에는 포함하지 않는다(PLAN.md D3).
    """

    access_token: str
    id_token: str | None
    refresh_token: str | None
    expires_in: int
    token_type: str
    sub: str
    member: User


@dataclass(frozen=True)
class RefreshResult:
    """
    토큰 갱신 성공 결과.

    refresh_token은 Cognito가 Refresh Token Rotation으로 새 값을
    돌려준 경우에만 채워진다(현재 dev App Client는 rotation 비활성
    이므로 보통 None). None이면 router는 기존 refresh_token 쿠키를
    그대로 둔다.
    """

    access_token: str
    id_token: str | None
    expires_in: int
    token_type: str
    refresh_token: str | None


def _authentication_result(response: dict, *, operation: str) -> dict:
    """
    Cognito InitiateAuth 응답에서 AuthenticationResult를 꺼낸다.

    MFA/NEW_PASSWORD_REQUIRED 등 챌린지가 필요한 사용자의 경우
    Cognito는 AuthenticationResult 대신 ChallengeName을 반환한다.
    Cognito 챌린지 처리는 PLAN.md §15에서 명시적으로 범위 밖이므로,
    이 경우를 성공으로 위장하지 않고 502(인증 서비스 오류)로 실패
    시킨다. 로그에는 챌린지 이름만 남기고 토큰/세션 값은 남기지
    않는다.
    """
    auth_result = response.get("AuthenticationResult") or {}

    if not auth_result.get("AccessToken"):
        challenge = response.get("ChallengeName")
        logger.error(
            "Cognito %s did not return an access token (challenge=%s)",
            operation,
            challenge,
        )
        status_code, detail = DEFAULT_CLIENT_ERROR
        raise CognitoApiError(status_code, detail)

    return auth_result


def _require_refresh_token(auth_result: dict) -> None:
    """
    로그인 성공에는 RefreshToken이 필수다 (PLAN.md D3/§4.2).

    최종 로그인 계약은 access/id token은 body로, refresh token은
    HttpOnly 쿠키로 내려주는 것이다. USER_PASSWORD_AUTH가
    AccessToken은 반환했지만 RefreshToken이 없다면(App Client 설정
    미스매치 등 정상적으로는 발생하지 않아야 하는 상태), 쿠키를 아예
    설정할 수 없는 반쪽짜리 세션이 만들어진다. 이를 성공으로
    위장하지 않고, Cognito 응답 원문을 노출하지 않는 502(인증 서비스
    오류)로 실패시킨다.
    """
    if not auth_result.get("RefreshToken"):
        logger.error(
            "Cognito InitiateAuth returned an access token but no refresh token"
        )
        status_code, detail = DEFAULT_CLIENT_ERROR
        raise CognitoApiError(status_code, detail)


def _resolve_member(sub: str, user_repository: UserRepository) -> User:
    """
    Cognito sub로 member를 조회하고 로그인 가능한 상태인지 검사한다.

    검사 순서와 의미는 app/api/deps.py의 get_current_member와 동일하게
    맞춘다(탈퇴가 종착 상태이므로 PENDING보다 먼저 검사한다). 다르게
    두면 "로그인은 되는데 /users/me는 403" 같은 불일치가 생긴다.
    """
    try:
        member_id = uuid.UUID(sub)
    except (ValueError, AttributeError, TypeError) as e:
        raise InvalidCognitoIdentityError(
            "Cognito returned a sub that is not a valid UUID"
        ) from e

    member = user_repository.get_by_id(member_id)
    if member is None:
        raise MemberNotFoundError("Member not found for the authenticated user")

    if member.status == MemberStatus.WITHDRAWN or member.deleted_at is not None:
        raise MemberWithdrawnError("This member has been withdrawn")

    if member.status == MemberStatus.PENDING:
        raise MemberEmailNotVerifiedError("Email verification has not been completed")

    return member


def log_in(
    *,
    email: str,
    password: str,
    user_repository: UserRepository,
) -> LoginResult:
    """
    POST /auth/login 오케스트레이션 (PLAN.md §4.2).

    흐름:
      1. email 정규화(signup/availability와 동일한 strip + lower)
      2. Cognito InitiateAuth(USER_PASSWORD_AUTH, SECRET_HASH=f(email))
      3. RefreshToken이 응답에 없으면 502로 실패(§_require_refresh_token
         참고, D3: 로그인 성공에는 refresh_token 쿠키가 필수다)
      4. 발급된 Access Token으로 Cognito GetUser -> sub 확보
         (토큰 문자열을 서명 검증 없이 직접 파싱하지 않는다)
      5. sub로 member 조회 + 상태 검사
         ACTIVE -> 성공 / PENDING -> 403 / WITHDRAWN -> 403 / 없음 -> 404

    Cognito ClientError/EndpointConnectionError는
    app/core/cognito_errors.py의 단일 매핑을 통해 CognitoApiError로
    변환해 전파한다(endpoint마다 error_code를 분기하지 않는다).
    NotAuthorizedException과 UserNotFoundException이 동일한 401 +
    동일 문구가 되는 것도 그 매핑 테이블이 보장한다.
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    normalized_email = _normalize_email(email)

    try:
        response = initiate_password_auth(
            email=normalized_email, password=password
        )
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        logger.info("Cognito InitiateAuth rejected login: error_code=%s", error_code)
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        logger.error("Cognito InitiateAuth: could not reach Cognito")
        raise connection_error_to_exception() from e

    auth_result = _authentication_result(response, operation="InitiateAuth")
    _require_refresh_token(auth_result)

    try:
        sub = get_user_sub(access_token=auth_result["AccessToken"])
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        logger.error("Cognito GetUser failed after login: error_code=%s", error_code)
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        logger.error("Cognito GetUser: could not reach Cognito")
        raise connection_error_to_exception() from e

    member = _resolve_member(sub, user_repository)

    return LoginResult(
        access_token=auth_result["AccessToken"],
        id_token=auth_result.get("IdToken"),
        refresh_token=auth_result.get("RefreshToken"),
        expires_in=auth_result.get("ExpiresIn", 0),
        token_type=auth_result.get("TokenType", "Bearer"),
        sub=sub,
        member=member,
    )


def refresh_session(*, refresh_token: str, sub: str) -> RefreshResult:
    """
    POST /auth/refresh(쿠키 기반, 최종 계약) 오케스트레이션
    (PLAN.md §4.3).

    SECRET_HASH의 username으로는 반드시 refresh_sub 쿠키 값(=Cognito
    sub)을 사용한다. refresh token은 opaque 문자열이라 여기서 sub를
    파싱할 수 없기 때문이다(로그인 시 쿠키로 함께 내려둔 이유).

    이 경로는 DB를 조회하지 않는다. 갱신 시점의 member 상태 검사는
    이후 access token으로 호출되는 /users/me 등에서
    app/api/deps.py의 get_current_member가 이미 담당하고 있으며,
    여기서 중복 검사를 추가하면 PLAN.md에 없는 새 정책(예: 탈퇴
    직후 갱신 차단)을 임의로 만들게 된다.
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    try:
        response = refresh_auth(refresh_token=refresh_token, sub=sub)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        logger.info("Cognito RefreshToken rejected: error_code=%s", error_code)
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        logger.error("Cognito RefreshToken: could not reach Cognito")
        raise connection_error_to_exception() from e

    auth_result = _authentication_result(response, operation="RefreshToken")

    return RefreshResult(
        access_token=auth_result["AccessToken"],
        id_token=auth_result.get("IdToken"),
        expires_in=auth_result.get("ExpiresIn", 0),
        token_type=auth_result.get("TokenType", "Bearer"),
        refresh_token=auth_result.get("RefreshToken"),
    )


def log_out(*, refresh_token: str) -> bool:
    """
    POST /auth/logout 오케스트레이션 (PLAN.md §4.3).

    Cognito RevokeToken을 호출하되, 실패해도 예외를 던지지 않고
    False를 반환한다. 로그아웃은 사용자 관점에서 멱등해야 하며,
    로컬 쿠키 삭제와 204 응답은 Cognito 호출 결과와 무관하게 항상
    수행되어야 하기 때문이다(호출자가 분기하지 않아도 되도록 여기서
    흡수한다).

    반환값(revoke 성공 여부)은 로깅/테스트 용도이며 HTTP 응답에는
    영향을 주지 않는다. 실패 시 error_code만 남기고 refresh token
    값은 절대 로그에 남기지 않는다.
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    try:
        revoke_refresh_token(refresh_token=refresh_token)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        logger.warning(
            "Cognito RevokeToken failed; clearing local cookies anyway "
            "(error_code=%s)",
            error_code,
        )
        return False
    except EndpointConnectionError:
        logger.warning(
            "Cognito RevokeToken: could not reach Cognito; "
            "clearing local cookies anyway"
        )
        return False
    except RuntimeError:
        # 신규 backend App Client 설정이 아직 주입되지 않은 환경
        # (_require_backend_client_credentials). 로그아웃이 이 이유로
        # 실패해서는 안 되므로 동일하게 흡수한다.
        logger.warning(
            "Cognito RevokeToken skipped: backend App Client is not configured; "
            "clearing local cookies anyway"
        )
        return False

    return True
