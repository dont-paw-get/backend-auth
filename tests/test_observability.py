"""
구조화 로깅 / 분산 추적 테스트 (app/core/logging_config.py,
app/core/tracing.py).

검증 범위:
- JSON 로그가 요구된 최소 필드(timestamp/level/service/logger/
  message/trace_id/span_id)를 항상 포함하는지
- 활성 span 안에서 남긴 로그의 trace_id/span_id가 그 span과
  일치하는지(Loki <-> Tempo 상관관계의 근거)
- 인증 서비스 금지 목록(password/Authorization/access·id·refresh
  token/JWT/Cognito secret/AWS credential/쿠키/이메일)이 stdout으로
  나가기 전에 마스킹되는지
- OTLP endpoint가 없으면 tracing이 조용히 비활성화되고, 설정 중
  예외가 나더라도 기동을 막지 않는지

tests/test_audit_log.py가 "애초에 민감값을 logger에 넘기지 않는다"는
1차 방어선을 검증한다면, 이 파일은 그 정책이 깨졌을 때의 2차
안전망(redact)을 검증한다.
"""

import json
import logging
import pathlib
import re

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.core import tracing
from app.core.audit_log import audit
from app.core.logging_config import (
    AccessLogPathFilter,
    JsonLogFormatter,
    RedactingTextFormatter,
    TEXT_FORMAT,
    configure_logging,
    current_trace_ids,
    redact,
    safe_extra,
    service_name,
)

REQUIRED_FIELDS = (
    "timestamp",
    "level",
    "service",
    "logger",
    "message",
    "trace_id",
    "span_id",
)


def _record(message="hello", *, level=logging.INFO, name="app.test", **extra):
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _format(record) -> dict:
    return json.loads(JsonLogFormatter().format(record))


@pytest.fixture()
def span_recorder():
    """
    전역 TracerProvider를 건드리지 않고 span을 만든다.

    OpenTelemetry는 전역 provider의 재설정을 허용하지 않으므로
    (한 번 설정되면 경고와 함께 무시된다), 테스트에서는 로컬
    provider의 tracer를 직접 쓴다. start_as_current_span은 전역
    provider와 무관하게 현재 context에 span을 붙이므로
    trace.get_current_span()으로 조회된다.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("tests"), exporter
    provider.shutdown()


# ---------------------------------------------------------------------------
# JSON 포맷: 최소 필드
# ---------------------------------------------------------------------------


class TestJsonLogFormat:
    def test_contains_all_required_fields(self):
        payload = _format(_record())

        for field in REQUIRED_FIELDS:
            assert field in payload, f"missing required field: {field}"

    def test_service_is_backend_auth_by_default(self, monkeypatch):
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        assert service_name() == "backend-auth"
        assert _format(_record())["service"] == "backend-auth"

    def test_service_follows_otel_service_name(self, monkeypatch):
        monkeypatch.setenv("OTEL_SERVICE_NAME", "backend-auth")
        assert _format(_record())["service"] == "backend-auth"

    def test_timestamp_is_utc_iso8601(self):
        timestamp = _format(_record())["timestamp"]
        assert timestamp.endswith("Z")
        assert "T" in timestamp

    def test_level_and_logger_are_recorded(self):
        payload = _format(_record("boom", level=logging.ERROR, name="app.audit"))
        assert payload["level"] == "ERROR"
        assert payload["logger"] == "app.audit"
        assert payload["message"] == "boom"

    def test_output_is_a_single_json_line(self):
        line = JsonLogFormatter().format(_record("multi\nline"))
        assert "\n" not in line
        assert json.loads(line)["message"] == "multi\nline"

    def test_extra_fields_are_flattened(self):
        payload = _format(_record(event="login", outcome="success"))
        assert payload["event"] == "login"
        assert payload["outcome"] == "success"

    def test_exception_is_serialized(self):
        try:
            raise ValueError("kaboom")
        except ValueError:
            import sys

            record = _record("failed")
            record.exc_info = sys.exc_info()

        payload = _format(record)
        assert "ValueError" in payload["exception"]


# ---------------------------------------------------------------------------
# trace 상관관계
# ---------------------------------------------------------------------------


class TestTraceCorrelation:
    def test_ids_are_null_outside_of_a_span(self):
        payload = _format(_record())
        assert payload["trace_id"] is None
        assert payload["span_id"] is None

    def test_ids_match_the_active_span(self, span_recorder):
        tracer, _ = span_recorder

        with tracer.start_as_current_span("unit") as span:
            payload = _format(_record())
            context = span.get_span_context()

        assert payload["trace_id"] == format(context.trace_id, "032x")
        assert payload["span_id"] == format(context.span_id, "016x")
        assert len(payload["trace_id"]) == 32
        assert len(payload["span_id"]) == 16

    def test_current_trace_ids_never_raises_without_a_span(self):
        assert current_trace_ids() == (None, None)


# ---------------------------------------------------------------------------
# 민감정보 마스킹 (2차 안전망)
# ---------------------------------------------------------------------------


class TestRedaction:
    JWT = (
        "eyJraWQiOiJhYmMiLCJhbGciOiJSUzI1NiJ9"
        ".eyJzdWIiOiIxMjMiLCJ0b2tlbl91c2UiOiJhY2Nlc3MifQ"
        ".c2lnbmF0dXJlLXZhbHVl"
    )

    def test_jwt_is_masked(self):
        masked = redact(f"token was {self.JWT} rejected")
        assert self.JWT not in masked
        assert "[REDACTED]" in masked

    def test_authorization_header_is_masked(self):
        masked = redact("Authorization: Bearer abc.def.ghi")
        assert "abc.def.ghi" not in masked

    def test_password_key_value_is_masked(self):
        for text in (
            "password=hunter2",
            'password: "hunter2"',
            "new_password=hunter2",
            "current_password = hunter2",
        ):
            assert "hunter2" not in redact(text), text

    def test_cognito_secret_and_secret_hash_are_masked(self):
        masked = redact("client_secret=abc123 secret_hash=ZmFrZWhhc2g=")
        assert "abc123" not in masked
        assert "ZmFrZWhhc2g=" not in masked

    def test_tokens_by_key_are_masked(self):
        masked = redact(
            "access_token=aaa id_token=bbb refresh_token=ccc session=ddd cookie=eee"
        )
        for value in ("aaa", "bbb", "ccc", "ddd", "eee"):
            assert value not in masked

    def test_aws_access_key_id_is_masked(self):
        masked = redact("used AKIAIOSFODNN7EXAMPLE for the call")
        assert "AKIAIOSFODNN7EXAMPLE" not in masked

    def test_email_local_part_is_masked_but_domain_survives(self):
        masked = redact("member very-identifiable@example.com failed")
        assert "very-identifiable" not in masked
        assert "example.com" in masked

    def test_member_id_is_not_masked_in_the_audit_log(self):
        """
        member_id(Cognito sub)는 지속적 사용자 식별자이지만, 감사
        로그에서는 조사에 필요한 유일한 상관관계 키이므로 마스킹하지
        않는다(단방향 해시로 바꾸면 DB와 조인할 수 없어 감사 로그가
        목적을 잃는다). 노출 범위는 "trace에는 올리지 않는다"로
        제한한다 — TestAuditStructuredFields 참고.
        """
        member_id = "11111111-2222-3333-4444-555555555555"
        assert member_id in redact(f"member_id={member_id}")

    def test_redaction_applies_to_json_message(self):
        payload = _format(_record(f"login failed for Bearer {self.JWT}"))
        assert self.JWT not in json.dumps(payload)

    def test_redaction_applies_to_extra_values(self):
        payload = _format(_record("ok", reason=f"token {self.JWT}"))
        assert self.JWT not in json.dumps(payload)

    def test_redaction_applies_to_text_format_too(self):
        line = RedactingTextFormatter(TEXT_FORMAT).format(
            _record(f"password=hunter2 {self.JWT}")
        )
        assert "hunter2" not in line
        assert self.JWT not in line


class TestSafeExtra:
    def test_sensitive_keys_are_dropped_entirely(self):
        result = safe_extra(
            {
                "member_id": "abc",
                "password": "hunter2",
                "access_token": "aaa",
                "refresh_token": "bbb",
                "email": "user@example.com",
                "code": "123456",
                "cookie": "session=1",
                "aws_secret_access_key": "x",
            }
        )
        assert result == {"member_id": "abc"}

    def test_logrecord_reserved_attributes_are_dropped(self):
        """
        logging은 extra=에 예약 속성이 들어오면 KeyError를 던진다.
        관측 필드 하나 때문에 로그 호출이 죽으면 안 된다.
        """
        result = safe_extra({"name": "x", "message": "y", "args": "z", "reason": "ok"})
        assert result == {"reason": "ok"}

    def test_reserved_key_in_extra_does_not_break_logging(self, caplog):
        """safe_extra를 거친 audit()은 어떤 키가 와도 예외를 내지 않는다."""
        with caplog.at_level(logging.INFO, logger="app.audit"):
            audit("login", outcome="failure", name="collides", reason="ok")

        assert any(r.name == "app.audit" for r in caplog.records)


# ---------------------------------------------------------------------------
# audit(): 구조화 필드 + span 속성
# ---------------------------------------------------------------------------


class TestAuditStructuredFields:
    def test_message_format_is_unchanged(self, caplog):
        """기존 감사 로그 형식(grep/알림 의존)을 깨지 않는다."""
        with caplog.at_level(logging.INFO, logger="app.audit"):
            audit("login", outcome="success", member_id="sub-1")

        message = caplog.records[-1].getMessage()
        assert "event=login" in message
        assert "outcome=success" in message
        assert "member_id=sub-1" in message

    def test_fields_are_also_emitted_as_json_fields(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.audit"):
            audit("login", outcome="failure", reason="email_not_verified")

        payload = _format(caplog.records[-1])
        assert payload["event"] == "login"
        assert payload["outcome"] == "failure"
        assert payload["reason"] == "email_not_verified"

    def test_current_span_gets_auth_attributes(self, span_recorder):
        tracer, exporter = span_recorder

        with tracer.start_as_current_span("POST /api/v1/auth/login"):
            audit("login", outcome="failure", reason="email_not_verified")

        attributes = exporter.get_finished_spans()[0].attributes
        assert attributes["auth.event"] == "login"
        assert attributes["auth.outcome"] == "failure"
        assert attributes["auth.reason"] == "email_not_verified"

    def test_member_id_stays_out_of_span_attributes(self, span_recorder):
        """
        Cognito sub는 지속적 사용자 식별자다. 감사 로그에는 남기되
        (보안 조사에 DB와 조인 가능한 식별자가 필요하다) trace에는
        올리지 않는다 — 같은 요청의 로그가 이미 동일한 trace_id를
        갖고 있어 trace 쪽 사본은 노출 확대일 뿐이다.
        """
        tracer, exporter = span_recorder

        with tracer.start_as_current_span("POST /api/v1/auth/login"):
            audit("login", outcome="success", member_id="sub-1")

        attributes = exporter.get_finished_spans()[0].attributes
        assert "auth.member_id" not in attributes
        assert "sub-1" not in str(dict(attributes))
        # 이벤트/결과 자체는 여전히 trace에서 조회 가능해야 한다.
        assert attributes["auth.event"] == "login"
        assert attributes["auth.outcome"] == "success"

    def test_member_id_is_still_recorded_in_the_audit_log(self, caplog):
        """span에서 뺀 것이 로그에서까지 빠지면 감사 기능이 무너진다."""
        with caplog.at_level(logging.INFO, logger="app.audit"):
            audit("login", outcome="success", member_id="sub-1")

        record = caplog.records[-1]
        assert "member_id=sub-1" in record.getMessage()
        assert _format(record)["member_id"] == "sub-1"

    def test_span_attributes_are_allowlisted_not_blocklisted(self, span_recorder):
        """
        허용 목록에 없는 새 필드는 자동으로 span에 실리지 않는다.
        나중에 식별자성 필드가 추가돼도 trace로 새어나가지 않게 하는
        안전장치다.
        """
        tracer, exporter = span_recorder

        with tracer.start_as_current_span("POST /api/v1/auth/login"):
            audit(
                "login",
                outcome="failure",
                reason="bad",
                password="hunter2",
                some_future_identifier="device-fingerprint-abc",
            )

        attributes = exporter.get_finished_spans()[0].attributes
        assert "auth.password" not in attributes
        assert "auth.some_future_identifier" not in attributes
        assert attributes["auth.reason"] == "bad"

    def test_works_without_an_active_span(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.audit"):
            audit("logout", outcome="success")

        assert caplog.records[-1].getMessage().startswith("auth_audit")


# ---------------------------------------------------------------------------
# tracing 활성화 게이트
# ---------------------------------------------------------------------------


class TestTracingGate:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """
        환경변수와 모듈 전역 상태를 모두 초기화한다.

        _tracing_configured까지 되돌리는 이유: 실제 collector 주소가
        주입된 환경에서 pytest를 돌리면 app.main import 시점에 이미
        tracing이 켜져 있어(configure_tracing이 조기 return) 이
        클래스의 검증이 무의미해진다. 앰비언트 환경과 무관하게
        게이트 자체를 테스트한다.
        """
        for name in (
            "OTEL_SDK_DISABLED",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(tracing, "_tracing_configured", False)

    def test_disabled_when_no_endpoint_is_configured(self):
        assert tracing.tracing_enabled() is False

    def test_enabled_when_endpoint_is_configured(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        assert tracing.tracing_enabled() is True

    def test_traces_specific_endpoint_also_enables(self, monkeypatch):
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector:4318/v1/traces"
        )
        assert tracing.tracing_enabled() is True

    def test_otel_sdk_disabled_wins(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        assert tracing.tracing_enabled() is False

    def test_blank_endpoint_does_not_enable(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
        assert tracing.tracing_enabled() is False

    def test_configure_tracing_is_a_noop_when_disabled(self):
        assert tracing.configure_tracing() is False

    def test_instrument_app_is_a_noop_when_tracing_is_off(self):
        """tracing이 꺼져 있으면 app에 어떤 미들웨어도 추가하지 않는다."""
        from app.main import app

        before = len(app.user_middleware)
        tracing.instrument_app(app)
        assert len(app.user_middleware) == before


class TestObservabilityNeverBreaksStartup:
    def test_configure_tracing_swallows_setup_failures(self, monkeypatch):
        """
        collector 주소가 있어도 SDK 설정이 실패하면 예외를 밖으로
        내보내지 않고 False만 돌려준다(인증 API 기동을 막지 않는다).
        """
        monkeypatch.setattr(tracing, "_tracing_configured", False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setattr(
            tracing,
            "_build_resource",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        assert tracing.configure_tracing() is False

    def test_instrumentation_failure_is_isolated(self, caplog):
        """instrumentation 하나가 실패해도 예외가 전파되지 않는다."""
        with caplog.at_level(logging.WARNING, logger="app.core.tracing"):
            tracing._instrument(
                "broken", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            )

        assert any("broken" in r.getMessage() for r in caplog.records)

    def test_configure_logging_is_idempotent(self):
        root = logging.getLogger()
        configure_logging()
        first = len(root.handlers)
        configure_logging()
        assert len(root.handlers) == first == 1

    def test_app_still_serves_requests_with_observability_configured(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200


class TestAccessLogPathFilter:
    """
    uvicorn.access 로그에서 probe(/health)·스크레이핑(/metrics)의 **성공**
    응답만 버린다. 트레이스/메트릭이 같은 두 경로를 이미 제외하고 있고,
    로그도 같은 정책으로 맞춘다(dev 실측상 access 로그의 ~91%가 이 두
    경로의 200). 실패(4xx/5xx)는 조사에 필요하므로 남긴다.
    """

    def _access_record(self, path, status=200):
        # uvicorn AccessFormatter가 기대하는 args 5-튜플
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:52000", "GET", path, "1.1", status),
            exc_info=None,
        )

    @pytest.mark.parametrize("path", ["/health", "/metrics", "/metrics?x=1"])
    @pytest.mark.parametrize("status", [200, 204, 301, 304])
    def test_successful_probe_and_scrape_logs_are_dropped(self, path, status):
        assert (
            AccessLogPathFilter().filter(self._access_record(path, status)) is False
        )

    @pytest.mark.parametrize("path", ["/health", "/metrics"])
    @pytest.mark.parametrize("status", [401, 404, 500, 503])
    def test_failed_probe_and_scrape_logs_are_kept(self, path, status):
        assert (
            AccessLogPathFilter().filter(self._access_record(path, status)) is True
        )

    @pytest.mark.parametrize(
        "path", ["/api/v1/auth/login", "/api/v1/users/me", "/healthz", "/", "/.env"]
    )
    def test_real_request_paths_pass_through(self, path):
        assert AccessLogPathFilter().filter(self._access_record(path)) is True

    def test_unexpected_record_shape_is_not_dropped(self):
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="something else entirely",
            args=None,
            exc_info=None,
        )
        assert AccessLogPathFilter().filter(record) is True

    def test_non_numeric_status_is_not_dropped(self):
        record = self._access_record("/health")
        record.args = ("127.0.0.1:52000", "GET", "/health", "1.1", "weird")
        assert AccessLogPathFilter().filter(record) is True

    def test_configure_logging_attaches_exactly_one_filter(self):
        configure_logging()
        configure_logging()
        access_logger = logging.getLogger("uvicorn.access")
        matching = [
            f for f in access_logger.filters if isinstance(f, AccessLogPathFilter)
        ]
        assert len(matching) == 1

    def test_filter_is_applied_at_logger_level_before_propagation(self):
        """
        필터를 uvicorn.access 로거에 붙였으므로, propagate로 root
        핸들러에 도달하기 전에 평가된다(Logger.handle이 callHandlers
        전에 filter를 부른다).
        """
        configure_logging()
        access_logger = logging.getLogger("uvicorn.access")
        # Logger.filter는 통과 시 record(또는 True)를, 차단 시 False를
        # 반환한다(Python 3.12+는 record를 그대로 돌려준다).
        assert not access_logger.filter(self._access_record("/health"))
        assert access_logger.filter(self._access_record("/api/v1/auth/login"))


# ---------------------------------------------------------------------------
# instrumentation 대상 범위 / 중복 계측
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestInstrumentationScope:
    """
    instrumentation은 애플리케이션 코드가 실제로 호출하는 라이브러리만
    켠다. httpx는 fastapi.testclient 전용 의존성이므로 제외한다 —
    outbound httpx 호출을 추가할 때 그 변경과 함께 다시 넣는다.
    """

    def test_production_code_does_not_instrument_httpx(self):
        source = (REPO_ROOT / "app" / "core" / "tracing.py").read_text(
            encoding="utf-8"
        )
        assert "HTTPXClientInstrumentor" not in source

    def test_httpx_instrumentation_is_not_a_declared_dependency(self):
        requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        declared = [
            line.split("==")[0].strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert "opentelemetry-instrumentation-httpx" not in declared

    def test_app_code_has_no_outbound_httpx_call(self):
        """
        위 두 테스트의 전제(애플리케이션에 outbound httpx 호출이 없다)가
        아직 유효한지 확인한다. httpx 호출을 추가하는 변경은 여기서
        실패하므로, instrumentation 을 함께 추가하라는 신호가 된다.
        """
        offenders = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "app").rglob("*.py")
            if re.search(r"^\s*(import|from)\s+httpx\b", path.read_text(encoding="utf-8"), re.M)
        ]
        assert offenders == [], (
            "app/ 에 httpx import 가 생겼다. outbound 호출이라면 "
            "opentelemetry-instrumentation-httpx 를 requirements.txt 와 "
            "app/core/tracing.py 의 _instrument_libraries 에 함께 추가할 것."
        )


class TestNoDuplicateServerSpan:
    """
    FastAPI instrumentation 과 ASGI OpenTelemetryMiddleware 가 겹쳐
    같은 요청에 SERVER span 이 두 개 생기지 않는지 확인한다.

    이 저장소는 FastAPIInstrumentor.instrument_app() 하나만 쓰고
    OpenTelemetryMiddleware 를 직접 add_middleware 하지 않는다. 아래
    테스트는 그 전제와, 실수로 두 번 계측하더라도 SERVER span 이
    늘어나지 않는다는 점을 실제 span 으로 검증한다.
    """

    @staticmethod
    def _server_spans(exporter):
        from opentelemetry.trace import SpanKind

        return [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]

    def test_app_adds_no_opentelemetry_asgi_middleware_of_its_own(self):
        """
        우리가 직접 붙이는 ASGI 미들웨어는 CORS 와 Prometheus 메트릭
        둘뿐이고, 그중 어느 것도 OpenTelemetryMiddleware 가 아니어야
        한다(FastAPI instrumentation 과 겹쳐 SERVER span 이 중복되면
        안 되기 때문 — 아래 클래스 docstring 참고).
        """
        source = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        assert "OpenTelemetryMiddleware" not in source
        # CORSMiddleware + PrometheusMiddleware 둘뿐이다.
        assert source.count("add_middleware") == 2

    def test_double_instrumentation_still_yields_one_server_span(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        probe_app = FastAPI()

        @probe_app.get("/ping")
        def ping():
            return {"ok": True}

        FastAPIInstrumentor.instrument_app(probe_app, tracer_provider=provider)
        # 두 번째 호출은 경고만 남기고 아무것도 하지 않아야 한다.
        FastAPIInstrumentor.instrument_app(probe_app, tracer_provider=provider)
        try:
            with TestClient(probe_app) as client:
                assert client.get("/ping").status_code == 200

            server_spans = self._server_spans(exporter)
            assert len(server_spans) == 1, (
                f"expected exactly 1 SERVER span, got {len(server_spans)}: "
                f"{[s.name for s in server_spans]}"
            )
        finally:
            FastAPIInstrumentor.uninstrument_app(probe_app)
            provider.shutdown()

    def test_our_instrument_app_is_guarded_and_repeatable(self):
        """
        app/core/tracing.py 의 instrument_app 을 여러 번 불러도 예외가
        나지 않고 미들웨어 스택이 자라지 않는다(tracing 이 꺼진
        기본 테스트 환경에서는 아예 no-op).
        """
        from app.main import app

        before = len(app.user_middleware)
        tracing.instrument_app(app)
        tracing.instrument_app(app)
        assert len(app.user_middleware) == before
