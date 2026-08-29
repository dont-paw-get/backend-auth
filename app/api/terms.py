from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.terms_repository import TermsRepository
from app.schemas.terms import TermsResponse
from app.services.member_service import RequiredTermsNotConfiguredError
from app.services.terms_service import list_terms

router = APIRouter(tags=["terms"])


@router.get("/api/v1/terms", response_model=list[TermsResponse])
def list_terms_endpoint(db: Session = Depends(get_db)):
    """
    현재 적용 중인 약관 목록을 공개 조회한다 (CLIAR-176).

    회원가입 화면에서 FE가 약관 원문을 보여주기 위한 endpoint다.
    로그인하지 않은 사용자도 호출해야 하므로 인증을 요구하지
    않는다(Bearer/Cookie 어느 쪽도 검사하지 않음 — 다른 인증
    endpoint의 정책은 전혀 건드리지 않는다).

    응답은 TERMS_OF_SERVICE -> PRIVACY -> AI_ANALYSIS 순으로
    고정된다(app/services/terms_service.py 참고).

    필수 약관(TERMS_OF_SERVICE, PRIVACY) 중 하나라도 없으면 503을
    반환한다 — POST /api/v1/auth/signup이 같은 상황에서 사용하는
    RequiredTermsNotConfiguredError(app/services/member_service.py)
    를 그대로 재사용해 동일하게 매핑한다(회원가입 API와 회원가입
    화면 약관 표시가 같은 서버 설정 문제를 서로 다른 상태 코드로
    보고하지 않도록). AI_ANALYSIS는 선택 약관이므로 없어도 200을
    유지하고 그냥 목록에서 빠진다.
    """
    terms_repository = TermsRepository(db)
    try:
        return list_terms(terms_repository)
    except RequiredTermsNotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        )
