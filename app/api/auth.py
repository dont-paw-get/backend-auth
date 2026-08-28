import logging

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.cognito_errors import CognitoApiError
from app.core.cookies import (
    REFRESH_SUB_COOKIE_NAME,
    REFRESH_TOKEN_COOKIE_NAME,
    clear_refresh_cookies,
    set_refresh_cookies,
)
from app.core.database import get_db
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    AvailabilityRequest,
    AvailabilityResponse,
    LoginRequest,
    LoginResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    SignupConfirmRequest,
    SignupConfirmResponse,
    SignupRequest,
    SignupResendRequest,
    SignupResponse,
)
from app.schemas.user import MemberResponse
from app.services.auth_service import check_availability, refresh_access_token
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


@router.post("/login", response_model=LoginResponse)
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
        # detail을 dict로 내려 FE가 "이메일 인증 미완료"를 기계적으로
        # 구분할 수 있게 한다(app/api/deps.py의 get_current_member,
        # cognito_errors의 UserNotConfirmedException 매핑과 동일한 형태).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=EMAIL_NOT_VERIFIED_DETAIL,
        )
    except MemberWithdrawnError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except MemberNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except InvalidCognitoIdentityError:
        # 사용자 입력 문제가 아니라 Cognito/서버 구성 문제다. Cognito
        # 원문을 노출하지 않는 일반 메시지로 응답한다.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authenticated identity is not a valid UUID",
        )
    except CognitoApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

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
    payload: RefreshTokenRequest | None = Body(default=None),
    refresh_token_cookie: str | None = Cookie(
        default=None, alias=REFRESH_TOKEN_COOKIE_NAME
    ),
    refresh_sub_cookie: str | None = Cookie(
        default=None, alias=REFRESH_SUB_COOKIE_NAME
    ),
):
    """
    Cognito Refresh Token으로 새 Access Token을 재발급한다.

    최종 계약(CLIAR-153, Phase 4): **request body 없음**. refresh_token
    과 refresh_sub HttpOnly 쿠키만 사용하며, 신규 backend App
    Client(secret 있음) + SECRET_HASH=f(refresh_sub)로 Cognito
    REFRESH_TOKEN_AUTH를 호출한다. 새 Refresh Token이 반환되면
    (rotation 활성화 시) refresh_token 쿠키를 갱신한다.

    LEGACY(CLIAR-125, Phase 7에서 제거): refresh_token/refresh_sub
    쿠키가 **둘 다** 없고 request body에 refresh_token이 있으면, 기존
    FE 계약을 깨지 않기 위해 예전 경로(기존 FE App Client, SECRET_HASH
    없음)로 그대로 처리한다. 기존 body 방식에 신규 backend client
    secret을 억지로 적용하지 않는다(그렇게 하면 기존 FE가 발급받은
    refresh token이 즉시 거부된다).

    두 쿠키 중 **하나라도** 존재하면 그 시점부터 쿠키 모드로
    간주한다: 완전한 쌍(둘 다 있음)이면 신규 경로를 진행하고, 한쪽만
    있으면(예: refresh_sub만 남고 refresh_token만 만료/삭제된 상태)
    legacy body 존재 여부와 무관하게 401 + 두 쿠키 clear로 실패시킨다.
    쿠키가 하나라도 있는 상태에서 legacy body로 조용히 넘어가면,
    브라우저가 들고 있는 새 쿠키 계약과 서버가 실제로 검증한 자격
    증명이 어긋난 채로 로그인 세션이 유지되는 결과가 된다.

    Access Token이 만료됐을 때 사용하는 API이므로 이 endpoint는
    Bearer Access Token 인증을 요구하지 않는다.
    """
    if refresh_token_cookie or refresh_sub_cookie:
        # ---- 최종 계약: 쿠키 기반 ----
        if not (refresh_token_cookie and refresh_sub_cookie):
            # 두 쿠키는 항상 한 쌍으로 발급/삭제된다. 한쪽만 남아 있다면
            # (legacy body가 함께 왔더라도) 더 이상 갱신에 쓸 수 없는
            # 상태이므로 legacy로 넘기지 않고 남은 쿠키를 정리한다.
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

    # ---- LEGACY 경로 (Phase 7에서 이 블록 전체를 제거) ----
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh session cookies are missing",
        )

    try:
        return refresh_access_token(payload.refresh_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cognito rejected the refresh token",
        )
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not refresh the access token",
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
        log_out(refresh_token=refresh_token_cookie)

    clear_refresh_cookies(response)
    return None


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
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
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except (InvalidNicknameError, RequiredConsentNotAgreedError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RequiredTermsNotConfiguredError as e:
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except CognitoApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConfirmPersistenceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except CognitoApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    return SignupConfirmResponse(
        member_id=member.member_id,
        email=member.email,
        status=member.status.value,
    )


@router.post("/signup/resend", status_code=status.HTTP_204_NO_CONTENT)
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
