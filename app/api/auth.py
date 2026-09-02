import logging

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.audit_log import audit
from app.core.cognito_errors import CognitoApiError
from app.core.config import settings
from app.core.cookies import (
    REFRESH_SUB_COOKIE_NAME,
    REFRESH_TOKEN_COOKIE_NAME,
    clear_refresh_cookies,
    set_refresh_cookies,
)
from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.core.security import (
    bearer_scheme,
    get_current_access_token,
    get_current_user_id,
)
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    AvailabilityRequest,
    AvailabilityResponse,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RefreshTokenResponse,
    SignupConfirmRequest,
    SignupConfirmResponse,
    SignupRequest,
    SignupResendRequest,
    SignupResponse,
)
from app.schemas.user import MemberResponse
from app.services.auth_service import check_availability
from app.services.login_service import (
    InvalidCognitoIdentityError,
    MemberEmailNotVerifiedError,
    MemberNotFoundError,
    MemberWithdrawnError,
    log_in,
    log_out,
    refresh_session,
)
from app.services.member_service import (
    InvalidNicknameError,
    RequiredConsentNotAgreedError,
    RequiredTermsNotConfiguredError,
)
from app.services.password_service import (
    change_password,
    forgot_password,
    reset_password,
)
from app.services.signup_service import (
    ConfirmPersistenceError,
    EmailAlreadyRegisteredError,
    MemberNotFoundForConfirmError,
    SignupData,
    SignupPersistenceError,
    confirm_signup,
    resend_signup_code,
    sign_up_member,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/availability", response_model=AvailabilityResponse)
def check_availability_endpoint(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """
    회원가입 전 이메일/닉네임 중복 확인.

    Jira 요구사항상 잘못된 요청(지원하지 않는 field, 빈/공백 value,
    필수 키 누락 등)은 모두 HTTP 400이어야 한다. FastAPI/Pydantic의
    기본 body validation을 그대로 사용하면 422가 반환되므로,
    여기서는 raw dict로 body를 받아 직접 검증하여 400으로 통일한다.
    """
    try:
        request = AvailabilityRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user_repository = UserRepository(db)
    return check_availability(request.field, request.value, user_repository)


EMAIL_NOT_VERIFIED_DETAIL = {
    "code": "EMAIL_NOT_VERIFIED",
    "message": "Email verification has not been completed",
}


def _unauthorized_clearing_refresh_cookies(detail: str) -> JSONResponse:
    """
    401을 응답하면서 refresh_token/refresh_sub 쿠키를 함께 삭제한다.

    HTTPException을 raise하면 FastAPI가 새 응답 객체를 만들기 때문에,
    endpoint에 주입된 Response에 걸어둔 Set-Cookie 헤더가 유실된다.
    쿠키 삭제와 401을 함께 보내야 하는 경로에서는 이렇게 응답 객체를
    직접 만들어 반환한다.
    """
    response = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": detail}
    )
    clear_refresh_cookies(response)
    return response


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[Depends(rate_limit("login", settings.RATE_LIMIT_LOGIN))],
)
def login_endpoint(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    이메일 + 비밀번호로 로그인한다 (CLIAR-153, PLAN.md §4.2).

    backend-auth가 Cognito InitiateAuth(USER_PASSWORD_AUTH)를 직접
    호출하므로 FE는 Cognito SDK를 알 필요가 없다. 아직 로그인하지
    않은 사용자가 호출하는 API이므로 Bearer 인증을 요구하지 않는다.

    응답 body에는 access_token/id_token/expires_in/token_type/member만
    담고, Refresh Token과 Cognito sub는 HttpOnly 쿠키
    (refresh_token, refresh_sub)로만 내려준다(PLAN.md D3).

    실제 오케스트레이션(Cognito 호출, sub 확보, member 상태 판정)은
    app/services/login_service.py가 담당하며, 이 함수는 request
    parsing과 예외 -> HTTP status 변환, 쿠키 설정만 수행한다.
    """
    user_repository = UserRepository(db)

    try:
        result = log_in(
            email=payload.email,
            password=payload.password.get_secret_value(),
            user_repository=user_repository,
        )
    except MemberEmailNotVerifiedError:
        audit("login", outcome="failure", reason="email_not_verified")
        # detail을 dict로 내려 FE가 "이메일 인증 미완료"를 기계적으로
        # 구분할 수 있게 한다(app/api/deps.py의 get_current_member,
        # cognito_errors의 UserNotConfirmedException 매핑과 동일한 형태).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=EMAIL_NOT_VERIFIED_DETAIL,
        )
    except MemberWithdrawnError as e:
        audit("login", outcome="failure", reason="withdrawn")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except MemberNotFoundError as e:
        audit("login", outcome="failure", reason="member_not_found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except InvalidCognitoIdentityError:
        audit("login", outcome="failure", reason="invalid_cognito_identity")
        # 사용자 입력 문제가 아니라 Cognito/서버 구성 문제다. Cognito
        # 원문을 노출하지 않는 일반 메시지로 응답한다.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authenticated identity is not a valid UUID",
        )
    except CognitoApiError as e:
        audit("login", outcome="failure", reason=f"cognito_error_{e.status_code}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    audit("login", outcome="success", member_id=result.sub)

    if result.refresh_token:
        set_refresh_cookies(
            response, refresh_token=result.refresh_token, sub=result.sub
        )

    return LoginResponse(
        access_token=result.access_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        id_token=result.id_token,
        member=MemberResponse.model_validate(result.member),
    )


@router.post("/refresh", response_model=RefreshTokenResponse)
def refresh_token_endpoint(
    response: Response,
    refresh_token_cookie: str | None = Cookie(
        default=None, alias=REFRESH_TOKEN_COOKIE_NAME
    ),
    refresh_sub_cookie: str | None = Cookie(
        default=None, alias=REFRESH_SUB_COOKIE_NAME
    ),
):
    """
    Cognito Refresh Token으로 새 Access Token을 재발급한다
    (CLIAR-153/162, 최종 계약).

    **request body 없음**. request body가 오더라도 그 내용을 refresh
    token으로 사용하지 않는다 — refresh_token과 refresh_sub HttpOnly
    쿠키만 사용하며, 신규 backend App Client(secret 있음) +
    SECRET_HASH=f(refresh_sub)로 Cognito REFRESH_TOKEN_AUTH를
    호출한다. 새 Refresh Token이 반환되면(rotation 활성화 시)
    refresh_token 쿠키를 갱신한다.

    두 쿠키 중 하나라도 없으면(둘 다 없음, 또는 한쪽만 있음) 401을
    반환하고 남은 쿠키까지 함께 삭제한다 — 두 쿠키는 항상 한 쌍으로
    발급/삭제되므로, 한쪽만 있는 상태는 더 이상 갱신에 쓸 수 없다.

    CLIAR-125의 legacy body 기반 refresh(기존 FE App Client, request
    body의 refresh_token 사용)는 Phase 7에서 제거되었다 — FE가 이
    계약을 사용한 적이 없음이 확인되어, 신규 FE는 처음부터 쿠키
    기반 refresh만 사용한다.

    Access Token이 만료됐을 때 사용하는 API이므로 이 endpoint는
    Bearer Access Token 인증을 요구하지 않는다.
    """
    if not (refresh_token_cookie and refresh_sub_cookie):
        # CLIAR-232: refresh 401의 사유를 로그로 구분한다. 이 분기는
        # Cognito를 호출하기 전에 반환되므로 login_service의 error_code
        # 로그가 남지 않는다 — 로그만 보면 "왜 401인지" 알 수 없어
        # 원인 파악이 늦어졌다(실제 dev 장애 조사에서 확인). 토큰 값은
        # 절대 남기지 않고, 어느 쿠키가 있었는지 여부(bool)만 남긴다.
        logger.info(
            "refresh rejected: reason=missing_cookies "
            "has_refresh_token=%s has_refresh_sub=%s",
            bool(refresh_token_cookie),
            bool(refresh_sub_cookie),
        )
        return _unauthorized_clearing_refresh_cookies(
            "Refresh session cookies are missing or incomplete"
        )

    try:
        result = refresh_session(
            refresh_token=refresh_token_cookie, sub=refresh_sub_cookie
        )
    except CognitoApiError as e:
        if e.status_code == status.HTTP_401_UNAUTHORIZED:
            # NotAuthorizedException/UserNotFoundException: 이
            # refresh token으로는 더 이상 갱신할 수 없으므로 쿠키를
            # 지워 FE가 재로그인 흐름으로 넘어가게 한다.
            #
            # CLIAR-232: 쿠키는 정상적으로 있었지만 Cognito가 refresh
            # token을 거부한 경우다. "쿠키 누락"과 명확히 구분되는
            # 사유를 남겨 로그만으로 원인을 판별할 수 있게 한다.
            # 구체적 Cognito error_code는 login_service가 이미 남기며,
            # 여기서는 Cognito 원문 detail을 로그에 남기지 않는다.
            logger.info("refresh rejected: reason=cognito_rejected")
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            return _unauthorized_clearing_refresh_cookies(detail)
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    if result.refresh_token:
        # Refresh Token Rotation 대응. refresh_sub는 동일한 값으로
        # 다시 내려 유지한다.
        set_refresh_cookies(
            response,
            refresh_token=result.refresh_token,
            sub=refresh_sub_cookie,
        )

    return RefreshTokenResponse(
        access_token=result.access_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        id_token=result.id_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout_endpoint(
    response: Response,
    refresh_token_cookie: str | None = Cookie(
        default=None, alias=REFRESH_TOKEN_COOKIE_NAME
    ),
):
    """
    로그아웃한다 (CLIAR-153, PLAN.md §4.3). request body 없음.

    refresh_token 쿠키가 있으면 Cognito RevokeToken으로 해당 refresh
    token을 무효화한다. RevokeToken 성공 여부와 무관하게 로컬
    쿠키(refresh_token, refresh_sub)는 반드시 삭제하고 204를
    반환한다. 즉 사용자 관점에서 로그아웃은 항상 멱등하게 성공한다
    (쿠키가 아예 없어도 204).

    RevokeToken 실패는 app/services/login_service.py의 log_out()이
    error_code만 남기고 흡수한다. refresh token 값 자체는 어떤
    경로로도 로그에 남기지 않는다.
    """
    if refresh_token_cookie:
        revoked = log_out(refresh_token=refresh_token_cookie)
        # HTTP 응답은 revoke 성공 여부와 무관하게 항상 204이지만(위
        # docstring 참고), 감사 로그는 Cognito 측에서 실제로
        # 무효화됐는지를 별도로 남긴다 — 반복적인 revoke 실패는 보안
        # 관점에서 조사할 가치가 있는 신호이기 때문이다.
        audit(
            "logout",
            outcome="success" if revoked else "revoke_failed",
        )
    else:
        audit("logout", outcome="success", reason="no_session_cookie")

    clear_refresh_cookies(response)
    return None


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("signup", settings.RATE_LIMIT_SIGNUP))],
)
def signup_endpoint(
    payload: SignupRequest,
    db: Session = Depends(get_db),
):
    """
    Cognito SignUp + RDS member(PENDING) 생성 (CLIAR-151, PLAN.md §5).

    이 endpoint는 Bearer Access Token 인증을 요구하지 않는다(아직
    로그인하지 않은 사용자가 호출하는 API이기 때문). 실제 오케스트
    레이션(Cognito 호출, 이메일 중복 확인, 필수 약관 조회, member/
    member_agreement 생성, 실패 시 보상)은 전부
    app/services/signup_service.py가 담당하며, 이 함수는 request
    parsing과 예외 -> HTTP status 변환만 수행한다.

    CLIAR-144 정책: nickname 중복 검사를 하지 않는다.
    """
    user_repository = UserRepository(db)
    terms_repository = TermsRepository(db)
    member_agreement_repository = MemberAgreementRepository(db)

    data = SignupData(
        email=payload.email,
        password=payload.password.get_secret_value(),
        nickname=payload.nickname,
        birth_date=payload.birth_date,
        gender=payload.gender,
        agree_terms=payload.agree_terms,
        agree_privacy=payload.agree_privacy,
        agree_ai_analysis=payload.agree_ai_analysis,
    )

    try:
        member = sign_up_member(
            data,
            user_repository,
            terms_repository,
            member_agreement_repository,
        )
    except EmailAlreadyRegisteredError as e:
        audit("signup", outcome="failure", reason="email_already_registered")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except (InvalidNicknameError, RequiredConsentNotAgreedError) as e:
        audit("signup", outcome="failure", reason="validation_error")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RequiredTermsNotConfiguredError as e:
        audit("signup", outcome="failure", reason="terms_not_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        )
    except SignupPersistenceError as e:
        if e.compensation_failed:
            logger.error(
                "Signup DB failure and Cognito compensation (AdminDeleteUser) "
                "also failed; a possible orphan Cognito account remains "
                "for email hash unavailable here (see service logs)"
            )
        audit("signup", outcome="failure", reason="persistence_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except CognitoApiError as e:
        audit("signup", outcome="failure", reason=f"cognito_error_{e.status_code}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    audit("signup", outcome="success", member_id=str(member.member_id))

    return SignupResponse(
        member_id=member.member_id,
        email=member.email,
        status=member.status.value,
        code_delivery=None,
    )


@router.post("/signup/confirm", response_model=SignupConfirmResponse)
def signup_confirm_endpoint(
    payload: SignupConfirmRequest,
    db: Session = Depends(get_db),
):
    """
    Cognito ConfirmSignUp + RDS member(PENDING -> ACTIVE) 전이
    (CLIAR-151, PLAN.md §5).

    Cognito ConfirmSignUp이 성공한 뒤 DB UPDATE가 실패하면
    ConfirmPersistenceError -> 500으로 응답한다(성공한 것처럼 200을
    반환하지 않는다). DB에 해당 email의 member가 없으면
    MemberNotFoundForConfirmError -> 404로 응답한다.
    """
    user_repository = UserRepository(db)

    try:
        member = confirm_signup(
            email=payload.email,
            code=payload.code,
            user_repository=user_repository,
        )
    except MemberNotFoundForConfirmError as e:
        audit("signup_confirm", outcome="failure", reason="member_not_found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConfirmPersistenceError as e:
        audit("signup_confirm", outcome="failure", reason="persistence_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except CognitoApiError as e:
        audit(
            "signup_confirm",
            outcome="failure",
            reason=f"cognito_error_{e.status_code}",
        )
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    audit("signup_confirm", outcome="success", member_id=str(member.member_id))

    return SignupConfirmResponse(
        member_id=member.member_id,
        email=member.email,
        status=member.status.value,
    )


@router.post(
    "/signup/resend",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("signup_resend", settings.RATE_LIMIT_SIGNUP))],
)
def signup_resend_endpoint(payload: SignupResendRequest):
    """
    Cognito ResendConfirmationCode 재호출 (CLIAR-151, PLAN.md §5).

    사용자 존재 여부를 과도하게 노출하지 않는 기존 보안 정책은
    app/core/cognito_errors.py의 매핑 테이블(NotAuthorizedException/
    UserNotFoundException 동일 응답)에 이미 반영되어 있다.
    """
    try:
        resend_signup_code(email=payload.email)
    except CognitoApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    return None


@router.post(
    "/password/forgot",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("password_forgot", settings.RATE_LIMIT_PASSWORD))],
)
def password_forgot_endpoint(payload: PasswordForgotRequest):
    """
    비밀번호 재설정 코드를 이메일로 발송한다 (CLIAR-157, PLAN.md
    §4.4).

    가입 여부와 무관하게 항상 204를 반환한다. 미가입 이메일임을
    응답 차이로 노출하지 않기 위해서다(user enumeration 방지,
    §16). 실제 판단은 app/services/password_service.py의
    forgot_password가 UserNotFoundException을 흡수하는 방식으로
    수행하며, 이 함수는 그 결과를 그대로 204로 응답한다.

    TooManyRequests/LimitExceeded 등 그 외 Cognito 오류는 기존
    cognito_errors 매핑을 그대로 따른다(429/502 등).
    """
    try:
        forgot_password(email=payload.email)
    except CognitoApiError as e:
        audit(
            "password_forgot",
            outcome="failure",
            reason=f"cognito_error_{e.status_code}",
        )
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    audit("password_forgot", outcome="success")

    return None


@router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("password_reset", settings.RATE_LIMIT_PASSWORD))],
)
def password_reset_endpoint(payload: PasswordResetRequest):
    """
    인증 코드로 비밀번호를 재설정한다 (CLIAR-157, PLAN.md §4.4).

    아직 로그인하지 않은 사용자가 호출하는 API이므로 Bearer 인증을
    요구하지 않는다. CodeMismatchException/ExpiredCodeException/
    InvalidPasswordException 등은 모두 기존 cognito_errors 매핑을
    그대로 따른다.
    """
    try:
        reset_password(
            email=payload.email,
            code=payload.code,
            new_password=payload.new_password.get_secret_value(),
        )
    except CognitoApiError as e:
        audit(
            "password_reset",
            outcome="failure",
            reason=f"cognito_error_{e.status_code}",
        )
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    audit("password_reset", outcome="success")

    return None


@router.post(
    "/password/change",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(bearer_scheme)],
)
def password_change_endpoint(
    payload: PasswordChangeRequest,
    access_token: str = Depends(get_current_access_token),
    user_id: str = Depends(get_current_user_id),
):
    """
    로그인 상태에서 비밀번호를 변경한다 (CLIAR-157, PLAN.md §4.4).

    Bearer Access Token이 필수다. 인증 대상은 오직
    get_current_access_token이 검증한 토큰에서만 얻으며,
    request body는 current_password/new_password만 받는다 —
    client가 member_id/sub/email 등을 보내 인증 대상을 스스로
    결정하게 하지 않는다.

    get_current_user_id도 함께 Depends()하는 것은 감사 로그에 남길
    member_id(sub)만을 위해서다. 두 dependency 모두 동일한
    _extract_and_verify_bearer_token을 공유하므로(app/core/
    security.py), 같은 요청 안에서 실제 토큰 검증은 한 번만
    실행된다(추가 Cognito 호출 없음).
    """
    try:
        change_password(
            access_token=access_token,
            current_password=payload.current_password.get_secret_value(),
            new_password=payload.new_password.get_secret_value(),
        )
    except CognitoApiError as e:
        audit(
            "password_change",
            outcome="failure",
            member_id=user_id,
            reason=f"cognito_error_{e.status_code}",
        )
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    audit("password_change", outcome="success", member_id=user_id)

    return None
