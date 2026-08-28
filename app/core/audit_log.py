"""
인증 보안 감사 로그 공통 helper (CLIAR-160, Phase 6, PLAN.md §9.4).

signup/signup 이메일 인증/login/logout/password 관련 보안 이벤트를
한 곳에서 일관된 형식으로 기록한다. endpoint마다 임의의
`logger.info(f"...")` 문자열을 중복 작성하지 않기 위함이다
(app/core/cognito_errors.py가 Cognito 오류 -> HTTP 매핑을 한 곳에
모아둔 것과 동일한 목적).

이 로그는 보안 감사 목적이며 사용자 데이터 덤프가 아니다. 아래
값은 이 함수의 호출자가 **절대** 인자로 넘기지 않아야 한다:

- password / current_password / new_password
- confirmation code
- access token / id token / refresh token / refresh_sub
- client secret / SECRET_HASH
- Authorization 헤더 원문

기존 프로젝트 로깅 정책(app/core/cognito.py, app/core/cognito_auth.py,
app/services/*.py 전반)은 실패 로그에도 email 등 개인정보를 남기지
않고 sub(member_id)와 오류 분류만 남기는 것을 일관되게 지켜왔다. 이
모듈도 같은 정책을 따른다 — email은 마스킹 여부와 무관하게 audit
로그에도 남기지 않는다(member_id를 알 수 있는 시점에는 member_id로
충분히 상관관계를 추적할 수 있고, 아직 인증 전이라 member_id를 모르는
시점(로그인 실패, signup, password forgot/reset)에는 event/outcome/
reason만으로 이상 징후 탐지에 필요한 최소 정보를 제공한다).

호출자는 이 함수가 검증하지 않는 임의 keyword를 넘길 수 있지만, 그
값 자체가 위 금지 목록에 해당하지 않는지는 호출자의 책임이다.
"""

import logging

logger = logging.getLogger("app.audit")


def audit(event: str, *, outcome: str, **fields: object) -> None:
    """
    인증 보안 이벤트 한 건을 기록한다.

    event: "login", "signup", "signup_confirm", "logout",
        "password_forgot", "password_reset", "password_change" 등
        고정 이벤트 이름.
    outcome: "success" | "failure" | "requested" 등, 이벤트별로
        호출자가 결정하는 결과 값. HTTP status 자체가 아니라 도메인
        관점의 결과를 의도적으로 자유 문자열로 둔다(logout처럼 HTTP
        응답은 항상 204이지만 Cognito 측 revoke 성공/실패는 감사
        목적으로 구분해야 하는 경우가 있기 때문).
    fields: member_id, reason(예: "email_not_verified",
        "cognito_error_429") 등 안전한 추가 정보만 넘긴다.
    """
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    if detail:
        logger.info("auth_audit event=%s outcome=%s %s", event, outcome, detail)
    else:
        logger.info("auth_audit event=%s outcome=%s", event, outcome)
