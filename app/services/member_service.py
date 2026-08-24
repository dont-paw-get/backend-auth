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

from app.models.user import MemberStatus, User
from app.repositories.user_repository import UserRepository


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
) -> User:
    """
    신뢰된 사용자 식별 정보와 온보딩 데이터로 MEMBER를 최초 생성한다.

    검증 순서: nickname 정규화 -> 필수 동의 확인 -> 중복 검사(member_id/
    email, 모두 정규화된 값 기준) -> 생성. nickname은 CLIAR-87부터 중복을
    허용하므로 중복 검사 대상이 아니다. 검증 실패 시 어떤 DB row도
    추가되지 않는다.

    transaction 경계: 이 함수가 commit까지 책임진다. UserRepository.create
    는 add + flush만 수행해 애플리케이션 사전 중복 검사를 통과한 뒤에도
    발생할 수 있는 DB unique constraint 위반(동시 요청에 의한 경쟁
    조건)을 이 함수의 같은 트랜잭션 안에서 감지하고 롤백할 수 있게 한다.
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

    # CLIAR-87: agree_terms/agree_privacy/agree_ai_analysis/agreed_at은
    # member 테이블 컬럼에서 제거되었다(terms + member_agreement로 이관
    # 예정이지만, 실제 동의 이력 저장 API는 이번 CLIAR-87 범위가 아니다).
    # 필수 동의 검증 자체는 위에서 그대로 수행하고, member row에는
    # 더 이상 이 값들을 저장하지 않는다.
    member = User(
        member_id=identity.user_id,
        email=normalized_email,
        nickname=normalized_nickname,
        status=MemberStatus.ACTIVE,
    )

    try:
        user_repository.create(member)
        user_repository.db.commit()
    except IntegrityError:
        # 사전 중복 검사를 통과했더라도 동시 요청에 의해 DB unique
        # constraint(user_id/email/nickname)에 최종적으로 걸릴 수 있다.
        # 이 경우 트랜잭션을 롤백해 불완전한 row가 남지 않도록 한다.
        user_repository.db.rollback()
        raise MemberAlreadyExistsError(
            "Member could not be created due to a conflicting record"
        ) from None

    user_repository.db.refresh(member)
    return member
