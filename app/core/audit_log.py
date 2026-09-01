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

로그와 trace의 경계: member_id(Cognito sub)는 **감사 로그에만** 남고
trace(span 속성)에는 올라가지 않는다. 이유는 아래
_annotate_current_span의 docstring에 있다.
"""

import logging

from app.core.logging_config import safe_extra

try:  # pragma: no cover - opentelemetry는 requirements.txt에 포함되어 있다
    from opentelemetry import trace as _otel_trace
except ImportError:  # OTel 패키지가 없어도 감사 로그는 남아야 한다
    _otel_trace = None

logger = logging.getLogger("app.audit")


# span에 실을 수 있는 추가 필드의 **허용 목록**(차단 목록이 아니다).
#
# 여기 없는 필드는 로그에만 남고 trace에는 올라가지 않는다. 특히
# member_id(Cognito sub)가 여기 없는 것은 의도적이다 — 아래
# _annotate_current_span docstring의 "왜 member_id를 span에 넣지
# 않는가" 참고. 새 필드를 span에 올리려면 이 목록에 명시적으로
# 추가해야 하므로, 나중에 추가되는 식별자성 필드가 자동으로 trace에
# 흘러드는 일이 없다.
_SPAN_SAFE_FIELDS = frozenset({"reason"})


def _annotate_current_span(event: str, outcome: str, fields: dict[str, object]) -> None:
    """
    현재 활성 span(= FastAPI가 만든 inbound HTTP span)에 감사 이벤트를
    속성으로 붙인다.

    별도의 custom span을 만들지 않는 이유: login/refresh/signup은 각각
    endpoint 하나에 대응하므로, wrapper span을 추가하면 FastAPI가 이미
    만든 서버 span과 시작/종료 시각이 사실상 동일한 span이 하나 더
    생길 뿐이다(Cognito 호출은 botocore instrumentation이, DB 조회는
    sqlalchemy instrumentation이 이미 자식 span으로 만든다). 자동
    instrumentation이 못 주는 유일한 정보는 "이 요청이 도메인 관점에서
    어떤 인증 이벤트였고 결과가 무엇이었나"이며, 그것만 속성으로
    덧붙인다. 이렇게 하면 Tempo에서 `auth.event="login" AND
    auth.outcome="failure"` 같은 조건으로 실패한 인증 trace를 바로
    찾을 수 있다.

    왜 member_id(Cognito sub)를 span에 넣지 않는가
    ----------------------------------------------
    sub는 계정이 살아있는 동안 바뀌지 않는 **지속적 사용자 식별자**다.
    가명 식별자이긴 하나 member 테이블과 조인하면 곧바로 실제 사용자로
    환원되므로, "내부 식별자라서 비민감"이라고 단정할 수 없다. 따라서
    "어디에 남기는가"를 용도별로 나눈다.

    - **감사 로그(여기서 logger.info)**: member_id를 남긴다. 이 모듈의
      존재 이유 자체가 보안 감사이고("특정 계정에 대한 반복 인증
      실패", "탈취 의심 계정의 활동 추적"), 그 조사에는 DB와 조인
      가능한 식별자가 반드시 필요하다. 단방향 해시로 바꾸면 감사
      로그가 목적을 잃는다.
    - **trace(span 속성)**: 남기지 않는다. trace는 지연/오류를 보는
      성능 신호이고 사용자 식별이 필요 없다. 게다가 어차피 같은
      요청의 감사 로그가 동일한 trace_id를 갖고 있으므로, 조사자가
      trace에서 사용자를 알아야 하면 trace_id로 로그를 찾으면 된다 —
      식별자 사본을 trace에 한 벌 더 두는 것은 순수한 노출 확대다.
      trace는 성능 대시보드 성격상 감사 로그보다 열람 범위가 넓고
      보존 정책도 다르기 때문에 더욱 그렇다.

    결과적으로 span에 올라가는 값은 event/outcome/reason뿐이며, 셋 다
    고정된 열거형에 가까운 저카디널리티 값이라 사용자 식별에 쓸 수
    없다(카디널리티 폭증도 없다).

    관측 실패가 인증 API를 깨뜨리면 안 되므로 모든 예외를 흡수한다.
    """
    if _otel_trace is None:
        return
    try:
        span = _otel_trace.get_current_span()
        if not span.is_recording():
            return
        span.set_attribute("auth.event", event)
        span.set_attribute("auth.outcome", outcome)
        for key, value in fields.items():
            if key in _SPAN_SAFE_FIELDS:
                span.set_attribute(f"auth.{key}", str(value))
    except Exception:  # pragma: no cover - 방어적
        return


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
    safe_fields = safe_extra(fields)
    _annotate_current_span(event, outcome, safe_fields)

    # message 문자열 형식은 그대로 유지한다(기존 감사 로그 grep/알림과
    # tests/test_audit_log.py가 이 형식에 의존한다). 여기에 더해 JSON
    # 구조화 로그에서 개별 필드로도 조회할 수 있도록 extra=로 같은
    # 값을 넘긴다 — JsonLogFormatter가 이를 top-level 필드로 펼친다
    # (app/core/logging_config.py). safe_extra()가 민감 키와 LogRecord
    # 예약 속성 충돌을 미리 걸러낸다.
    extra = {"event": event, "outcome": outcome, **safe_fields}

    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    if detail:
        logger.info(
            "auth_audit event=%s outcome=%s %s", event, outcome, detail, extra=extra
        )
    else:
        logger.info("auth_audit event=%s outcome=%s", event, outcome, extra=extra)
