from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.user import Gender, MemberStatus


class MemberUpdateRequest(BaseModel):
    """
    현재 인증된 사용자의 프로필 부분 수정 요청 schema.

    CLIAR-87: agree_ai_analysis는 member 테이블 컬럼에서 제거되어
    더 이상 이 API로 수정할 수 없다.

    CLIAR-120: birth_date/gender도 부분 수정 가능하다. 둘 다 optional이며
    보내지 않은 필드는 기존 값을 유지한다(model_dump(exclude_unset=True)
    를 사용하는 기존 PATCH 흐름, app/api/users.py 참고).
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = None
    profile_image_url: str | None = None
    birth_date: date | None = None
    gender: Gender | None = None

    @field_validator("nickname")
    @classmethod
    def nickname_must_be_non_null_and_non_blank(
        cls, 
        value: str | None
    ) -> str:
        if value is None:
            raise ValueError("nickname must not be null")

        normalized = value.strip()

        if not normalized:
            raise ValueError("nickname must not be empty or blank")

        return normalized



class MemberResponse(BaseModel):
    """
    MEMBER 조회 응답 schema.

    CLIAR-87: user_id(str) -> member_id(UUID)로 변경. 약관 동의 관련
    필드(agree_ai_analysis 등)는 member 테이블에서 제거되어(terms +
    member_agreement로 이관) 더 이상 이 응답에 포함하지 않는다.

    CLIAR-120: birth_date/gender를 응답에 포함한다. DB 컬럼이
    nullable이므로(기존 row 호환), 두 필드 모두 Optional로 선언해
    기존 member(NULL 값)를 조회해도 serialization 오류 없이 null로
    응답한다.
    """

    model_config = ConfigDict(from_attributes=True)

    member_id: UUID
    email: str
    nickname: str
    profile_image_url: str | None
    birth_date: date | None
    gender: Gender | None
    status: MemberStatus
    created_at: datetime
    updated_at: datetime



class MemberBootstrapRequest(BaseModel):
    """
    Cognito 인증 완료 후 MEMBER 최초 생성 요청 schema.

    CLIAR-105: user_id/email은 더 이상 request body로 받지 않는다.
    Client가 임의의 user_id/email을 보내 서버의 identity 판단을
    좌우할 수 있는 구조는 최종 Cognito 인증 구조와 맞지 않기 때문이다.
    member_id(Cognito sub)와 email은 Authorization 헤더의 검증된
    Cognito Access Token과 Cognito GetUser 응답에서만 얻는다
    (app/api/users.py의 bootstrap_current_member 참고).

    nickname:
        서비스 nickname

    birth_date:
        생년월일(YYYY-MM-DD). CLIAR-120부터 신규 회원 bootstrap에서
        필수다(Optional이 아님). datetime.date 타입이므로 Pydantic이
        유효하지 않은 날짜(예: 2002-13-40)를 자동으로 거절한다.

    gender:
        성별(MALE/FEMALE만 허용). CLIAR-120부터 신규 회원 bootstrap에서
        필수다. DB 컬럼 자체는 기존 row 호환을 위해 nullable이지만,
        신규 가입 시에는 이 schema가 필수로 강제한다.

    agree_terms/privacy:
        필수 동의(값 검증만 하고, 실제 이력은 member_agreement에 저장한다)

    agree_ai_analysis:
        선택 동의
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None

    birth_date: date
    gender: Gender

    agree_terms: bool
    agree_privacy: bool
    agree_ai_analysis: bool = False