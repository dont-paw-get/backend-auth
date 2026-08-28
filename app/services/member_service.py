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
from datetime import date

from sqlalchemy.exc import IntegrityError

from app.models.member_agreement import MemberAgreementAction
from app.models.user import Gender, MemberStatus, User
from app.repositories.member_agreement_repository import MemberAgreementRepository
from app.repositories.terms_repository import TermsRepository
from app.repositories.user_repository import UserRepository

# CLIAR-113: 회원탈퇴 시 로그에 남길 수 있는 값(sub)만 사용하고, 이 모듈은
# Cognito SDK를 직접 다루지 않는다(bootstrap과 동일한 관례: Cognito 호출은
# app/api/users.py가 담당하고, 이 모듈은 DB 상태 전이와 commit 경계만
# 책임진다).

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
    """
    MEMBER 최초 생성 시 사용자가 입력하는 온보딩 데이터.

    CLIAR-120: birth_date/gender는 신규 bootstrap에서 필수이므로
    Optional이 아니다(app/schemas/user.py의 MemberBootstrapRequest가
    이미 필수로 검증하므로, 여기서는 그 결과를 그대로 전달받는다).
    """

    nickname: str | None
    birth_date: date
    gender: Gender
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


class MemberWithdrawalPersistenceError(MemberBootstrapError):
    """
    회원탈퇴 처리 중 member row의 상태 변경(commit)이 실패한 경우
    발생한다. 트랜잭션은 이미 rollback된 상태이며, 호출자는 이를
    사용자 입력 문제가 아닌 서버 오류로 변환해야 한다.
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
            birth_date=onboarding.birth_date,
            gender=onboarding.gender,
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


def start_withdrawal(member: User, user_repository: UserRepository) -> User:
    """
    회원탈퇴 1단계: member.status를 WITHDRAWN으로 변경하고 즉시
    commit한다.

    CLIAR-113 탈퇴 처리 순서:
      1. status=WITHDRAWN으로 변경 + commit (이 함수)
      2. 이 시점부터 GET/PATCH /users/me 등 일반 API 접근은
         get_current_member(app/api/deps.py)가 403으로 차단한다.
      3. Cognito DeleteUser 호출(이 함수 밖, app/api/users.py가 담당)
      4. Cognito 삭제 성공 후 deleted_at 기록(complete_withdrawal)

    이 단계와 Cognito DeleteUser 호출은 서로 다른 시스템에 대한 별도
    작업이므로 하나의 DB 트랜잭션으로 묶을 수 없다. 따라서 이 단계는
    독립적으로 commit하며, 이후 Cognito 호출이 실패해도 이미 커밋된
    WITHDRAWN 상태는 되돌리지 않는다(재시도는 status=WITHDRAWN,
    deleted_at=NULL 상태에서 DELETE /users/me를 다시 호출하는 것으로
    처리한다).

    이미 WITHDRAWN인 member(재시도 케이스, deleted_at 여부 무관)에
    대해서는 이 함수를 호출하지 않고 호출자가 그대로 다음 단계로
    진행해야 한다. 즉 이 함수는 status가 WITHDRAWN이 아닌 경우에만
    호출된다는 전제 하에 동작한다.

    출발 상태는 ACTIVE일 수도 PENDING(이메일 인증 대기)일 수도 있다.
    어느 쪽이든 WITHDRAWN으로 가는 전이는 동일하며, 이메일 인증을
    끝내지 않은 회원도 탈퇴할 수 있어야 한다.
    """
    try:
        user_repository.mark_withdrawn(member)
        user_repository.db.commit()
    except Exception as e:
        user_repository.db.rollback()
        raise MemberWithdrawalPersistenceError(
            "Failed to mark member as withdrawn"
        ) from e

    user_repository.db.refresh(member)
    return member


def complete_withdrawal(member: User, user_repository: UserRepository) -> User:
    """
    회원탈퇴 2단계: Cognito DeleteUser 성공 후 member.deleted_at을
    현재 UTC 시각으로 기록하고 commit한다.

    이 함수가 호출되기 전에 이미 member.status는 WITHDRAWN이어야
    한다(start_withdrawal 또는 이전 시도에서 이미 설정됨). 호출자
    (app/api/users.py)가 status=WITHDRAWN, deleted_at=NULL인 회원에
    대해서만 Cognito DeleteUser를 호출한 뒤 이 함수를 호출한다.
    """
    try:
        user_repository.mark_deleted_now(member)
        user_repository.db.commit()
    except Exception as e:
        user_repository.db.rollback()
        raise MemberWithdrawalPersistenceError(
            "Failed to record member deletion timestamp"
        ) from e

    user_repository.db.refresh(member)
    return member
