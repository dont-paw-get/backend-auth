"""
공개 약관 목록 조회 오케스트레이션 (CLIAR-176, GET /api/v1/terms).

app/api/terms.py는 request/response 변환만 담당하고, 정렬/필수 약관
검증 정책은 이 모듈이 담당한다(app/services/signup_service.py 등
다른 service와 동일한 책임 분리).
"""

from app.models.terms import Terms
from app.repositories.terms_repository import TermsRepository
from app.services.member_service import (
    AI_ANALYSIS_CODE,
    PRIVACY_CODE,
    TERMS_OF_SERVICE_CODE,
    RequiredTermsNotConfiguredError,
)

# 회원가입 화면에서 사용자가 읽는 자연스러운 순서(필수 약관 먼저,
# 그중에서도 이용약관 -> 개인정보, 마지막에 선택 약관). signup의
# 필수 약관 검증과 동일한 세 코드 상수를 그대로 재사용한다 — 새
# 코드 체계를 만들지 않는다.
_CODE_ORDER = (TERMS_OF_SERVICE_CODE, PRIVACY_CODE, AI_ANALYSIS_CODE)

# GET /api/v1/terms가 "필수"로 취급하는 code. AI_ANALYSIS_CODE는
# 여기 포함하지 않는다 — signup도 agree_ai_analysis=true일 때만
# 요구하는 선택 약관이므로, 단순 목록 조회에서까지 그 부재만으로
# 503을 내면 signup의 정책보다 더 엄격해진다.
_REQUIRED_CODES = (TERMS_OF_SERVICE_CODE, PRIVACY_CODE)


def _sort_key(terms: Terms) -> tuple[int, str]:
    """
    _CODE_ORDER에 있는 code는 그 순서대로, 그 외(향후 추가될 수 있는
    코드)는 뒤에 알파벳순으로 배치한다. 매 요청마다 순서가 바뀌지
    않도록(deterministic) code 자체를 2차 정렬 기준으로 둔다.
    """
    try:
        rank = _CODE_ORDER.index(terms.code)
    except ValueError:
        rank = len(_CODE_ORDER)
    return (rank, terms.code)


def list_terms(terms_repository: TermsRepository) -> list[Terms]:
    """
    현재 적용 중인 약관을 FE가 보여줄 고정된 순서로 정렬해 반환한다.

    필수 약관(TERMS_OF_SERVICE, PRIVACY) 중 하나라도 현재 적용 중인
    행이 없으면 RequiredTermsNotConfiguredError를 던진다 — signup
    (app/services/signup_service.py)이 동일한 상황에서 이미 이
    예외를 사용해 503으로 매핑하고 있으므로, 같은 의미의 새 예외를
    따로 만들지 않고 그대로 재사용한다. 두 endpoint가 "필수 약관이
    설정되지 않음"이라는 동일한 서버 설정 문제를 같은 예외/같은
    상태 코드로 보고해야 일관적이기 때문이다(회원가입 화면에
    필수 약관 동의 UI 자체를 못 띄우는 상태에서 signup만 별도로
    503을 내는 것은 FE 입장에서 원인 파악을 더 어렵게 만든다).

    AI_ANALYSIS는 선택 약관이므로 없어도 예외를 던지지 않고 그냥
    빠진 채로 반환한다(signup도 agree_ai_analysis=true일 때만
    요구하므로 동일한 정책).
    """
    current = sorted(terms_repository.list_current(), key=_sort_key)

    present_codes = {terms.code for terms in current}
    missing_required = [
        code for code in _REQUIRED_CODES if code not in present_codes
    ]
    if missing_required:
        raise RequiredTermsNotConfiguredError(
            f"No current terms configured for code(s)={missing_required!r}"
        )

    return current
