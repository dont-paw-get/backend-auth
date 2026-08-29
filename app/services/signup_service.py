"""
BE 주도 회원가입 오케스트레이션 (CLIAR-151, Phase 3).

app/api/auth.py의 signup/confirm/resend endpoint는 request parsing과
이 모듈 호출, 그리고 예외를 HTTPException으로 변환하는 역할만 담당한다.
Cognito 호출 + DB 저장이 뒤섞인 복합 흐름은 모두 여기에 둔다.

member/member_agreement 생성과 필수 약관 조회 로직은
_create_pending_member(아래)가 담당한다. member.status를 PENDING으로
생성한다는 점을 제외하면(이메일 인증 전이므로), 검증 순서와
commit/rollback 경계는 member_service.py의 회원탈퇴 로직과 동일한
관례(트랜잭션 하나로 처리, 실패 시 전체 rollback)를 따른다.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from app.core.cognito_auth import (
    admin_delete_user,
    admin_get_user,
    confirm_sign_up,
    extract_sub_from_admin_get_user,
    resend_confirmation_code,
    sign_up,
)
from app.core.cognito_errors import (
    cognito_client_error_to_exception,
    connection_error_to_exception,
)
from app.models.member_agreement import MemberAgreementAction
from app.models.user import Gender, MemberStatus, User
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository
from app.services.member_service import (
    AI_ANALYSIS_CODE,
    PRIVACY_CODE,
    TERMS_OF_SERVICE_CODE,
    RequiredConsentNotAgreedError,
    RequiredTermsNotConfiguredError,
    _normalize_email,
    _normalize_nickname,
)


class SignupError(Exception):
    """회원가입 오케스트레이션 관련 도메인 오류의 공통 베이스."""


class EmailAlreadyRegisteredError(SignupError):
    """이미 ACTIVE 또는 PENDING인 member가 해당 이메일을 사용 중인 경우."""


class SignupPersistenceError(SignupError):
    """
    Cognito SignUp은 성공했지만 DB(member/member_agreement) 저장이
    실패한 경우 발생한다. 호출자는 이를 500으로 매핑해야 한다.

    compensation_failed: AdminDeleteUser 보상 삭제 자체도 실패했는지
    여부. True인 경우 Cognito에 고아 계정이 남아있을 수 있으므로,
    운영 로그로 추적 가능해야 한다(router가 logger.error로 남긴다).
    """

    def __init__(self, message: str, *, compensation_failed: bool):
        super().__init__(message)
        self.compensation_failed = compensation_failed


class MemberNotFoundForConfirmError(SignupError):
    """confirm 시 DB에 해당 email의 member row가 없는 경우."""


class ConfirmPersistenceError(SignupError):
    """
    Cognito ConfirmSignUp은 성공했지만 DB(status=PENDING -> ACTIVE)
    UPDATE가 실패한 경우 발생한다. 호출자는 이를 500으로 매핑해야
    하며, 성공한 것처럼 200을 반환하면 안 된다(Cognito=CONFIRMED,
    DB=PENDING으로 어긋난 상태는 app/api/deps.py의 get_current_member
    가 이후 403 EMAIL_NOT_VERIFIED로 방어한다).
    """


@dataclass(frozen=True)
class SignupData:
    """POST /auth/signup 요청 필드를 서비스 계층으로 전달하기 위한 값 객체."""

    email: str
    password: str
    nickname: str | None
    birth_date: date
    gender: Gender
    agree_terms: bool
    agree_privacy: bool
    agree_ai_analysis: bool = False


def _create_pending_member(
    *,
    member_id: uuid.UUID,
    email: str,
    nickname: str,
    birth_date: date,
    gender: Gender,
    agree_terms: bool,
    agree_privacy: bool,
    agree_ai_analysis: bool,
    user_repository: UserRepository,
    terms_repository: TermsRepository,
    member_agreement_repository: MemberAgreementRepository,
) -> User:
    """
    member row(status=PENDING)와 필수/선택 약관 AGREE 이력을 하나의
    트랜잭션으로 생성한다.

    status는 ACTIVE가 아니라 PENDING으로 생성한다(이메일 인증 전).
    commit/rollback 경계는 예외 시 전체 rollback, 성공 시 한 번만
    commit하는 패턴이다. 이 함수는 email/member_id 중복 검사를 하지
    않는다(호출자가 이미 수행했다는 전제).
    """
    try:
        terms_of_service = terms_repository.get_current_by_code(TERMS_OF_SERVICE_CODE)
        if terms_of_service is None:
            raise RequiredTermsNotConfiguredError(
                f"No current terms configured for code={TERMS_OF_SERVICE_CODE!r}"
            )

        privacy = terms_repository.get_current_by_code(PRIVACY_CODE)
        if privacy is None:
            raise RequiredTermsNotConfiguredError(
                f"No current terms configured for code={PRIVACY_CODE!r}"
            )

        ai_analysis = None
        if agree_ai_analysis:
            ai_analysis = terms_repository.get_current_by_code(AI_ANALYSIS_CODE)
            if ai_analysis is None:
                raise RequiredTermsNotConfiguredError(
                    f"No current terms configured for code={AI_ANALYSIS_CODE!r}"
                )

        member = User(
            member_id=member_id,
            email=email,
            nickname=nickname,
            birth_date=birth_date,
            gender=gender,
            status=MemberStatus.PENDING,
        )
        user_repository.create(member)

        member_agreement_repository.create(
            member_id=member_id,
            terms_id=terms_of_service.id,
            action=MemberAgreementAction.AGREE,
        )
        member_agreement_repository.create(
            member_id=member_id,
            terms_id=privacy.id,
            action=MemberAgreementAction.AGREE,
        )
        if ai_analysis is not None:
            member_agreement_repository.create(
                member_id=member_id,
                terms_id=ai_analysis.id,
                action=MemberAgreementAction.AGREE,
            )

        user_repository.db.commit()
    except Exception:
        user_repository.db.rollback()
        raise

    user_repository.db.refresh(member)
    return member


def sign_up_member(
    data: SignupData,
    user_repository: UserRepository,
    terms_repository: TermsRepository,
    member_agreement_repository: MemberAgreementRepository,
) -> User:
    """
    POST /auth/signup 오케스트레이션 (PLAN.md §4.1, §5).

    흐름:
      1. email 정규화, nickname 정규화, 필수 동의 검증(모두
         member_service의 기존 헬퍼 재사용 — nickname 중복 검사는
         하지 않는다, CLIAR-144 최종 정책)
      2. DB에서 기존 email 상태 확인
         - ACTIVE/PENDING member가 이미 존재 -> EmailAlreadyRegisteredError
         - WITHDRAWN member가 존재하는 경우: PLAN.md는 이 케이스를
           명시적으로 다루지 않는다(§4.1 orphan recovery는 Cognito
           UsernameExistsException 발생 시의 흐름만 다루며, "DB에 이미
           WITHDRAWN row가 있다"는 경우는 별도로 언급되지 않는다).
           현재 코드 정책상 member.email은 UNIQUE 컬럼이므로, WITHDRAWN
           row가 있는 채로 새 member row를 또 만들면 email UNIQUE
           제약 위반(IntegrityError)이 발생한다. 이 케이스의 정책
           (재가입 허용 여부, 허용한다면 기존 row 재활용 여부)은
           PLAN.md/현재 코드 어디에도 결정되어 있지 않으므로, 이번
           구현은 이 케이스를 EmailAlreadyRegisteredError(409)로
           처리한다 — 임의로 재가입을 허용하는 새 정책을 만들지
           않기 위한 보수적 선택이며, 완료 보고에 명시한다.
      3. Cognito SignUp 호출
         - 성공 -> UserSub를 member_id로 PENDING member 생성
         - UsernameExistsException -> orphan recovery(§_recover_from_username_exists)
         - 그 외 ClientError/EndpointConnectionError -> cognito_errors
           매핑을 그대로 전파(CognitoApiError)
      4. DB 저장 실패 시 AdminDeleteUser로 보상 삭제 시도
    """
    normalized_email = _normalize_email(data.email)
    normalized_nickname = _normalize_nickname(data.nickname)

    if not data.agree_terms or not data.agree_privacy:
        raise RequiredConsentNotAgreedError(
            "agree_terms and agree_privacy must both be true to sign up"
        )

    existing_member = user_repository.get_by_email(normalized_email)
    if existing_member is not None and existing_member.status != MemberStatus.WITHDRAWN:
        # ACTIVE 또는 PENDING. CLIAR-144 정책: PENDING email도 Cognito
        # User Pool에서 이미 점유된 상태이므로 "이미 가입됨"으로 취급.
        raise EmailAlreadyRegisteredError(
            f"Email {normalized_email!r} is already registered"
        )
    if existing_member is not None and existing_member.status == MemberStatus.WITHDRAWN:
        # 위 docstring 참고: PLAN.md/기존 정책에 명시되지 않은 케이스.
        # 보수적으로 409 처리한다.
        raise EmailAlreadyRegisteredError(
            f"Email {normalized_email!r} was previously withdrawn; "
            "re-registration policy is not yet defined"
        )

    from botocore.exceptions import ClientError, EndpointConnectionError

    try:
        cognito_response = sign_up(email=normalized_email, password=data.password)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "UsernameExistsException":
            return _recover_from_username_exists(
                normalized_email=normalized_email,
                normalized_nickname=normalized_nickname,
                data=data,
                user_repository=user_repository,
                terms_repository=terms_repository,
                member_agreement_repository=member_agreement_repository,
            )
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        raise connection_error_to_exception() from e

    user_sub = cognito_response["UserSub"]
    member_id = uuid.UUID(user_sub)

    try:
        member = _create_pending_member(
            member_id=member_id,
            email=normalized_email,
            nickname=normalized_nickname,
            birth_date=data.birth_date,
            gender=data.gender,
            agree_terms=data.agree_terms,
            agree_privacy=data.agree_privacy,
            agree_ai_analysis=data.agree_ai_analysis,
            user_repository=user_repository,
            terms_repository=terms_repository,
            member_agreement_repository=member_agreement_repository,
        )
    except RequiredTermsNotConfiguredError:
        # 사용자 입력 문제도 일반 DB 장애도 아니라 서버(운영) 설정
        # 문제다. 이 경우에도 Cognito 계정은 이미 만들어졌으므로 고아
        # 계정을 남기지 않기 위해 동일하게 보상 삭제를 시도하지만,
        # 예외 타입 자체는 그대로 다시 던져 호출자(router)가 503으로
        # 매핑할 수 있게 한다(SignupPersistenceError/500으로 감싸지
        # 않는다).
        try:
            admin_delete_user(email=normalized_email)
        except Exception:
            pass
        raise
    except Exception as db_error:
        # Cognito SignUp 성공 + 그 외 DB 실패 -> 고아 계정 보상 삭제
        # (PLAN.md §4.1 "③ 실패 시 보상"). AdminDeleteUser 자체가
        # 실패하더라도 원래 DB 실패를 성공으로 위장하지 않는다.
        compensation_failed = False
        try:
            admin_delete_user(email=normalized_email)
        except Exception:
            compensation_failed = True

        raise SignupPersistenceError(
            "Cognito SignUp succeeded but member creation failed",
            compensation_failed=compensation_failed,
        ) from db_error

    return member


def _recover_from_username_exists(
    *,
    normalized_email: str,
    normalized_nickname: str,
    data: SignupData,
    user_repository: UserRepository,
    terms_repository: TermsRepository,
    member_agreement_repository: MemberAgreementRepository,
) -> User:
    """
    Cognito UsernameExistsException 발생 시 orphan recovery
    (PLAN.md §4.1).

    A. DB에 ACTIVE/PENDING member가 이미 존재 -> 409
       (이 함수 호출 전에 sign_up_member가 이미 DB를 조회했지만,
       Cognito 쪽 사용자가 그 사이 생성됐을 수 있는 경쟁 상태를
       고려해 AdminGetUser 이후에도 다시 확인한다)
    B. Cognito에는 사용자 존재, DB member 없음(고아) ->
       AdminGetUser로 sub 확보 -> 그 sub로 PENDING member 생성
       -> ResendConfirmationCode -> 201에 해당하는 member 반환

    AdminGetUser는 cognito-idp:AdminGetUser IAM 권한이 필요하다. 이
    권한이 아직 워크로드에 연결되지 않은 환경에서는 이 함수 자체가
    ClientError(AccessDeniedException 등)로 실패할 수 있다(완료 보고
    참고). 그 경우도 cognito_client_error_to_exception으로 매핑해
    전파한다(502로 귀결될 가능성이 높다).
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    try:
        admin_response = admin_get_user(email=normalized_email)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        raise connection_error_to_exception() from e

    cognito_sub = extract_sub_from_admin_get_user(admin_response)
    member_id = uuid.UUID(cognito_sub)

    existing_member = user_repository.get_by_id(member_id)
    if existing_member is not None and existing_member.status != MemberStatus.WITHDRAWN:
        # A. 이미 정상적으로 가입 절차가 진행 중이거나 완료된 계정.
        raise EmailAlreadyRegisteredError(
            f"Email {normalized_email!r} is already registered"
        )

    # B. Cognito orphan: Cognito에는 사용자가 있지만 DB에는 없다
    # (또는 WITHDRAWN 상태로만 남아있다). 그 sub로 새 PENDING member를
    # 생성한다.
    member = _create_pending_member(
        member_id=member_id,
        email=normalized_email,
        nickname=normalized_nickname,
        birth_date=data.birth_date,
        gender=data.gender,
        agree_terms=data.agree_terms,
        agree_privacy=data.agree_privacy,
        agree_ai_analysis=data.agree_ai_analysis,
        user_repository=user_repository,
        terms_repository=terms_repository,
        member_agreement_repository=member_agreement_repository,
    )

    try:
        resend_confirmation_code(email=normalized_email)
    except Exception:
        # 인증 코드 재전송 실패는 member 생성 자체를 되돌릴 이유가
        # 아니다(사용자가 /auth/signup/resend로 다시 시도할 수 있다).
        # 다만 이 실패를 숨기지 않고 그대로 전파해 500으로 응답한다.
        raise

    return member


def confirm_signup(
    *,
    email: str,
    code: str,
    user_repository: UserRepository,
) -> User:
    """
    POST /auth/signup/confirm 오케스트레이션 (PLAN.md §4.1, §5).

    흐름: Cognito ConfirmSignUp -> 성공 시 DB에서 email로 member 조회
    -> status PENDING -> ACTIVE로 UPDATE -> commit.

    이미 ACTIVE인 member가 confirm을 재호출하는 경우: Cognito
    ConfirmSignUp은 이미 CONFIRMED인 사용자에 대해
    NotAuthorizedException(또는 유사 오류)을 던지는 것이 일반적인
    Cognito 동작이며, 그 경우 이 함수는 ClientError를 그대로
    전파한다(호출자가 cognito_errors 매핑으로 처리). Cognito가 예외
    없이 성공을 반환하는 예외적인 경우에도, 이 함수는 이미 ACTIVE인
    member의 status를 다시 ACTIVE로 설정할 뿐 부작용이 없다(멱등).

    DB에 해당 email의 member가 없는 경우
    (MemberNotFoundForConfirmError)와, DB UPDATE 자체가 실패하는
    경우(ConfirmPersistenceError)를 명확히 구분해서 호출자가 각각
    적절한 HTTP status로 매핑할 수 있게 한다. 성공하지 않았는데 200을
    반환하는 일은 없다.
    """
    normalized_email = _normalize_email(email)

    from botocore.exceptions import ClientError, EndpointConnectionError

    try:
        confirm_sign_up(email=normalized_email, confirmation_code=code)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        raise connection_error_to_exception() from e

    member = user_repository.get_by_email(normalized_email)
    if member is None:
        raise MemberNotFoundForConfirmError(
            f"Cognito confirmed {normalized_email!r} but no matching member exists"
        )

    try:
        member.status = MemberStatus.ACTIVE
        user_repository.db.commit()
    except Exception as e:
        user_repository.db.rollback()
        raise ConfirmPersistenceError(
            "Cognito ConfirmSignUp succeeded but updating member status failed"
        ) from e

    user_repository.db.refresh(member)
    return member


def resend_signup_code(*, email: str) -> None:
    """
    POST /auth/signup/resend 오케스트레이션.

    Cognito ResendConfirmationCode를 호출한다. ClientError/
    EndpointConnectionError는 cognito_errors 매핑을 통해 전파한다.
    사용자 존재 여부를 과도하게 노출하지 않는 기존 보안 정책
    (cognito_errors.py의 NotAuthorizedException/UserNotFoundException
    동일 응답 원칙)은 매핑 테이블 자체에 이미 반영되어 있으므로 이
    함수가 추가로 처리할 것은 없다.
    """
    normalized_email = _normalize_email(email)

    from botocore.exceptions import ClientError, EndpointConnectionError

    try:
        resend_confirmation_code(email=normalized_email)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        raise cognito_client_error_to_exception(error_code) from e
    except EndpointConnectionError as e:
        raise connection_error_to_exception() from e
