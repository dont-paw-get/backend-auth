"""
Cognito 인증 완료 후 MEMBER를 최초 생성하는 service 계층.

이번 Jira는 FastAPI endpoint를 만들지 않는다. user_id(Cognito sub 역할)와
email은 이미 신뢰된 인증 계층에서 전달된 값이라고 가정하며, 이 모듈은
HTTP Header나 Cognito SDK를 전혀 다루지 않는다. 추후 실제 API Gateway
연동이 확정되면, 인증된 identity를 이 함수로 전달하는 얇은 API
endpoint만 추가하면 된다.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError

from app.models.member_agreement import MemberAgreementAction
from app.models.user import MemberStatus, User
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository

# CLIAR-92: 회원 최초 생성 시 동의 이력을 남길 약관 code.
TERMS_OF_SERVICE_CODE = "TERMS_OF_SERVICE"
PRIVACY_CODE = "PRIVACY"
AI_ANALYSIS_CODE = "AI_ANALYSIS"


@dataclass(frozen=True)
class TrustedIdentity:
    """
    이미 인증 계층(추후 API Gateway/Cognito)에서 신뢰된 사용자 식별 정보.

    이 값들은 클라이언트가 직접 입력한 값이 아니라, 인증이 끝난 이후에만
    전달된다고 가정한다. user_id는 Cognito sub이며, member.member_id
    (UUID)에 그대로 저장된다.
    """

    user_id: uuid.UUID
    email: str


@dataclass(frozen=True)
class OnboardingData:
    """MEMBER 최초 생성 시 사용자가 입력하는 온보딩 데이터."""

    nickname: str | None
    agree_terms: bool
    agree_privacy: bool
    agree_ai_analysis: bool = False


class MemberBootstrapError(Exception):
    """MEMBER 최초 생성 관련 도메인 오류의 공통 베이스."""


class InvalidNicknameError(MemberBootstrapError):
    """nickname이 null/빈 문자열/공백 문자열인 경우 발생한다."""


class RequiredConsentNotAgreedError(MemberBootstrapError):
    """agree_terms 또는 agree_privacy가 false인 경우 발생한다."""


class RequiredTermsNotConfiguredError(MemberBootstrapError):
    """
    회원 생성에 필요한 약관(TERMS_OF_SERVICE/PRIVACY, 또는
    agree_ai_analysis=true인 경우의 AI_ANALYSIS)이 현재 적용 중인
    상태로 DB에 존재하지 않는 경우 발생한다.

    이는 사용자 입력 문제가 아니라 서버(운영) 설정 문제이므로, API
    계층에서는 400/409가 아니라 서버 설정 오류를 나타내는 상태로
    변환해야 한다.
    """


class MemberAlreadyExistsError(MemberBootstrapError):
    """동일 user_id의 MEMBER가 이미 존재하는 경우 발생한다."""


class EmailAlreadyExistsError(MemberBootstrapError):
    """다른 MEMBER가 이미 동일 email을 사용 중인 경우 발생한다."""


class NicknameAlreadyExistsError(MemberBootstrapError):
    """
    더 이상 사용되지 않는다(하위 호환을 위해 남겨둠).

    CLIAR-87 확정 요구사항: member.nickname은 UNIQUE 제약이 없으며
    중복을 허용한다. bootstrap_member는 더 이상 nickname 중복을
    이유로 이 예외를 발생시키지 않는다.
    """


def _normalize_email(email: str) -> str:
    """기존 auth_service.check_availability의 EMAIL 정규화 정책과 동일하게
    앞뒤 공백을 제거하고 소문자로 변환한다."""
    return email.strip().lower()


def _normalize_nickname(nickname: str | None) -> str:
    """
    기존 PATCH /users/me의 nickname validator와 동일한 정책을 적용한다.
    null/빈 문자열/공백 문자열은 모두 허용하지 않고, 정상 값은 strip한다.
    """
    if nickname is None:
        raise InvalidNicknameError("nickname must not be null")

    normalized = nickname.strip()
    if not normalized:
        raise InvalidNicknameError("nickname must not be empty or blank")
    return normalized


def bootstrap_member(
    identity: TrustedIdentity,
    onboarding: OnboardingData,
    user_repository: UserRepository,
    terms_repository: TermsRepository,
    member_agreement_repository: MemberAgreementRepository,
) -> User:
    """
    신뢰된 사용자 식별 정보와 온보딩 데이터로 MEMBER를 최초 생성하고,
    필수/선택 약관에 대한 AGREE 이력을 member_agreement에 저장한다.

    검증 순서: nickname 정규화 -> 필수 동의 확인 -> 중복 검사(member_id/
    email, 모두 정규화된 값 기준) -> 필수 약관(TERMS_OF_SERVICE, PRIVACY)
    조회 -> (agree_ai_analysis=true인 경우) AI_ANALYSIS 약관 조회 ->
    member 생성 -> 약관 AGREE 이력 생성. nickname은 CLIAR-87부터 중복을
    허용하므로 중복 검사 대상이 아니다. 검증 실패 시 어떤 DB row도
    추가되지 않는다.

    transaction 경계(CLIAR-92): member 생성부터 약관 AGREE 이력 저장까지
    전체를 이 함수 하나의 트랜잭션으로 처리하고 마지막에 한 번만
    commit한다. UserRepository.create/MemberAgreementRepository.create는
    모두 add + flush만 수행하므로, 중간 어느 단계에서 예외가 발생해도
    아직 commit되지 않은 상태이며 except 블록에서 rollback하면 member
    row까지 포함해 전부 되돌아간다.
    """
    normalized_email = _normalize_email(identity.email)
    normalized_nickname = _normalize_nickname(onboarding.nickname)

    if not onboarding.agree_terms or not onboarding.agree_privacy:
        raise RequiredConsentNotAgreedError(
            "agree_terms and agree_privacy must both be true to create a member"
        )

    if user_repository.get_by_id(identity.user_id) is not None:
        raise MemberAlreadyExistsError(
            f"Member with user_id={identity.user_id!r} already exists"
        )

    if user_repository.exists_by_email(normalized_email):
        raise EmailAlreadyExistsError(f"Email {normalized_email!r} is already in use")

    # CLIAR-87: member.nickname은 UNIQUE 제약이 없으며 중복을 허용한다.
    # 따라서 회원 최초 생성 시 nickname 중복을 이유로 거부하지 않는다.

    try:
        # 필수 약관은 member/agreement를 만들기 전에 먼저 조회해서,
        # 약관이 없을 때 불필요하게 member row를 만들었다가 되돌리는
        # 상황을 피한다(같은 트랜잭션이므로 결과는 동일하지만, 실패를
        # 최대한 일찍 감지하기 위함이다).
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
        if onboarding.agree_ai_analysis:
            ai_analysis = terms_repository.get_current_by_code(AI_ANALYSIS_CODE)
            if ai_analysis is None:
                raise RequiredTermsNotConfiguredError(
                    f"No current terms configured for code={AI_ANALYSIS_CODE!r}"
                )

        # CLIAR-87: agree_terms/agree_privacy/agree_ai_analysis/agreed_at은
        # member 테이블 컬럼에서 제거되었다(terms + member_agreement로
        # 이관됨). member row에는 더 이상 이 값들을 저장하지 않는다.
        member = User(
            member_id=identity.user_id,
            email=normalized_email,
            nickname=normalized_nickname,
            status=MemberStatus.ACTIVE,
        )
        user_repository.create(member)

        member_agreement_repository.create(
            member_id=identity.user_id,
            terms_id=terms_of_service.id,
            action=MemberAgreementAction.AGREE,
        )
        member_agreement_repository.create(
            member_id=identity.user_id,
            terms_id=privacy.id,
            action=MemberAgreementAction.AGREE,
        )
        if ai_analysis is not None:
            member_agreement_repository.create(
                member_id=identity.user_id,
                terms_id=ai_analysis.id,
                action=MemberAgreementAction.AGREE,
            )

        user_repository.db.commit()
    except IntegrityError:
        # 사전 중복 검사를 통과했더라도 동시 요청에 의해 DB unique
        # constraint(member_id/email)에 최종적으로 걸릴 수 있다. 이 경우
        # 트랜잭션을 롤백해 member/agreement 모두 남지 않도록 한다.
        user_repository.db.rollback()
        raise MemberAlreadyExistsError(
            "Member could not be created due to a conflicting record"
        ) from None
    except RequiredTermsNotConfiguredError:
        user_repository.db.rollback()
        raise
    except Exception:
        # 약관 AGREE 이력 저장 중 예상치 못한 오류가 발생해도 member
        # row가 DB에 남지 않도록 같은 트랜잭션을 롤백한다.
        user_repository.db.rollback()
        raise

    user_repository.db.refresh(member)
    return member
