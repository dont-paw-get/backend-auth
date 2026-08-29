"""
MEMBER 도메인 공통 helper와 회원탈퇴 service.

_normalize_email/_normalize_nickname과 그 예외들(InvalidNicknameError
등)은 signup(app/services/signup_service.py)이 그대로 재사용하는
공유 코드다. 약관 code 상수(TERMS_OF_SERVICE_CODE 등)도 동일하게
signup이 사용한다. bootstrap 관련 코드(TrustedIdentity/
OnboardingData/bootstrap_member 등)는 CLIAR-162 Phase 7에서
/users/bootstrap endpoint와 함께 제거되었다(BE 주도 회원가입인
/auth/signup으로 완전히 흡수됨).
"""

from app.models.user import User
from app.repositories.user_repository import UserRepository

# CLIAR-92: 회원 최초 생성 시 동의 이력을 남길 약관 code.
# signup_service.py가 그대로 재사용한다.
TERMS_OF_SERVICE_CODE = "TERMS_OF_SERVICE"
PRIVACY_CODE = "PRIVACY"
AI_ANALYSIS_CODE = "AI_ANALYSIS"


class MemberServiceError(Exception):
    """MEMBER 도메인 오류의 공통 베이스."""


class InvalidNicknameError(MemberServiceError):
    """nickname이 null/빈 문자열/공백 문자열인 경우 발생한다."""


class RequiredConsentNotAgreedError(MemberServiceError):
    """agree_terms 또는 agree_privacy가 false인 경우 발생한다."""


class RequiredTermsNotConfiguredError(MemberServiceError):
    """
    회원 생성에 필요한 약관(TERMS_OF_SERVICE/PRIVACY, 또는
    agree_ai_analysis=true인 경우의 AI_ANALYSIS)이 현재 적용 중인
    상태로 DB에 존재하지 않는 경우 발생한다.

    이는 사용자 입력 문제가 아니라 서버(운영) 설정 문제이므로, API
    계층에서는 400/409가 아니라 서버 설정 오류를 나타내는 상태로
    변환해야 한다.
    """


class MemberWithdrawalPersistenceError(MemberServiceError):
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
