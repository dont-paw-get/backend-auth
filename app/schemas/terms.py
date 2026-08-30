from pydantic import BaseModel, ConfigDict


class TermsResponse(BaseModel):
    """
    GET /api/v1/terms 응답 항목 schema (CLIAR-176).

    FE가 회원가입 화면에서 약관 원문을 보여주는 데 필요한 필드만
    노출한다. app/models/terms.py의 Terms에는 이 외에도 id/
    effective_at/expired_at/created_at/updated_at/deleted_at이
    있지만, 전부 내부 관리용(버전 이력 관리, 감사 목적)이라 공개
    응답에는 포함하지 않는다 — FE가 실제로 필요로 하는 것은 "지금
    보여줄 약관이 무엇인지"뿐이다.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    content: str
    is_required: bool
