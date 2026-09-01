"""
OpenTelemetry 분산 추적 설정
(Application -> OTLP -> OpenTelemetry Collector -> Grafana Tempo).

설계 원칙
---------
1. **관측 때문에 인증 API가 실패하지 않는다.** collector가 죽어
   있거나, OTel 패키지 일부가 없거나, instrumentation 하나가
   예외를 던져도 이 모듈은 그 예외를 삼키고 애플리케이션을 정상
   기동시킨다. span export는 BatchSpanProcessor가 백그라운드
   스레드에서 비동기로 수행하므로, collector 장애는 요청 처리
   경로를 차단하지 않고 span만 드롭된다.

2. **endpoint를 하드코딩하지 않는다.** 표준 OTEL_* 환경변수만
   사용한다.

       OTEL_SERVICE_NAME=backend-auth
       OTEL_EXPORTER_OTLP_ENDPOINT=<collector endpoint>
       OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<environment>

   OTEL_EXPORTER_OTLP_ENDPOINT(또는 traces 전용 변수)가 주입되지
   않은 환경 — 로컬 개발, pytest — 에서는 tracing 전체를 켜지
   않는다. "collector 주소를 모르는데 일단 켜서 매번 연결 실패
   로그를 남기는" 상태를 만들지 않기 위해서다. 즉 이 파일은 dev/
   prod configmap에 endpoint가 들어오는 순간 자동으로 활성화된다.

3. **W3C trace context.** 다른 MSA가 보낸 traceparent/tracestate를
   그대로 이어받고(inbound), Cognito/AWS·outbound HTTP 호출에도
   전파한다(outbound). 전역 propagator를 명시적으로 설정하는
   이유는 botocore instrumentation이 의존성으로 함께 설치하는
   AWS X-Ray propagator가 기본값을 바꾸지 않도록 못박기 위해서다.

exporter 프로토콜
-----------------
OTLP **http/protobuf**(기본 포트 4318)를 사용한다. 선택 근거는 두
가지다.

1. **서비스 간 프로토콜 통일.** 여러 MSA가 같은 collector로 보내는
   구조에서 서비스마다 gRPC/HTTP가 섞이면 collector receiver 설정,
   NetworkPolicy, 장애 시 확인 절차가 서비스마다 갈라진다. 플랫폼
   전체가 하나의 OTLP 전송 방식(http/protobuf, 4318)을 쓰면 그
   설정과 운영 절차가 한 벌로 유지된다.
2. **런타임 의존성 단순화.** gRPC exporter는 grpcio라는 네이티브
   확장 의존성을 런타임에 추가로 끌어들인다. http/protobuf exporter는
   순수 파이썬 + protobuf만 요구하므로, 인증 서비스의 런타임 의존성
   표면과 이미지 크기를 불필요하게 늘리지 않는다.

collector에는 otlp receiver의 http endpoint가 열려 있어야 한다.
gRPC 포트(4317)를 가리키면 export가 실패한다.

sensitive data
--------------
span attribute에도 로그와 동일한 정책을 적용한다.
- HTTP 헤더 캡처(OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*)를
  켜지 않는다 -> Authorization 헤더/Cookie가 span에 실리지 않는다.
- request/response body는 어떤 instrumentation도 수집하지 않는다.
- SQLAlchemy instrumentation은 SQL 문만 기록하고 bind parameter는
  기록하지 않는다(email 등 값이 span에 들어가지 않는다).
- botocore instrumentation은 operation 이름/region/request id만
  기록하고 InitiateAuth의 AuthParameters(password, SECRET_HASH,
  refresh token)는 기록하지 않는다.
"""

import logging
import os

# 로그의 service 필드와 trace의 service.name이 어긋나면 Loki <-> Tempo
# 상관관계가 끊기므로, 두 곳이 같은 함수를 공유한다.
from app.core.logging_config import service_name

logger = logging.getLogger(__name__)

# FastAPI span에서 제외할 URL(정규식 부분일치). health probe는 10초
# 간격으로 들어오며 추적 가치가 없다. 환경변수로 덮어쓸 수 있다.
_DEFAULT_EXCLUDED_URLS = "health"

_tracing_configured = False


def tracing_enabled() -> bool:
    """
    OTLP endpoint가 주입되어 있고 SDK가 비활성화되지 않았는지 확인한다.

    OTEL_SDK_DISABLED는 OTel 표준 환경변수다(true면 전면 비활성).
    """
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    return bool(endpoint and endpoint.strip())


def _build_resource():
    """
    service.name 등 리소스 속성을 구성한다.

    Resource.create()가 OTEL_RESOURCE_ATTRIBUTES를 먼저 읽어들이고,
    여기서 넘기는 service.name이 그 위에 덮인다. OTEL_SERVICE_NAME이
    주입되어 있으면 그 값을, 없으면 "backend-auth"를 쓴다 — 환경변수
    누락으로 Tempo에 unknown_service로 쌓이는 일을 막는다.
    """
    from opentelemetry.sdk.resources import Resource

    return Resource.create({"service.name": service_name()})


def _set_w3c_propagator() -> None:
    """
    전역 propagator를 W3C trace context + baggage로 고정한다.

    OTEL_PROPAGATORS 환경변수가 명시적으로 주입된 경우에는 SDK가
    이미 그 설정을 반영했으므로 건드리지 않는다(운영자가 의도적으로
    다른 조합을 넣었을 수 있다).
    """
    if os.getenv("OTEL_PROPAGATORS", "").strip():
        return

    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )


def _instrument(name: str, action) -> None:
    """
    instrumentation 하나를 적용하되, 실패해도 기동을 막지 않는다.

    instrumentation 패키지 버전이 라이브러리 버전과 어긋나거나
    (fastapi/sqlalchemy 업그레이드 직후 등) 이미 적용된 상태에서
    재적용될 때 예외가 날 수 있다. 그 경우 해당 계층의 span만
    포기하고 나머지는 계속 적용한다.
    """
    try:
        action()
    except Exception:
        logger.warning("OpenTelemetry instrumentation failed: %s", name, exc_info=True)


def configure_tracing() -> bool:
    """
    TracerProvider + OTLP exporter + 라이브러리 instrumentation을
    설정한다. 실제로 활성화됐으면 True를 반환한다.

    FastAPI(ASGI) instrumentation만은 app 객체가 필요하므로
    instrument_app()에서 따로 적용한다.
    """
    global _tracing_configured

    if _tracing_configured:
        return True

    if not tracing_enabled():
        logger.info(
            "OpenTelemetry tracing disabled "
            "(OTEL_EXPORTER_OTLP_ENDPOINT is not configured)"
        )
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=_build_resource())
        # BatchSpanProcessor: export는 백그라운드 스레드에서 수행되며
        # 큐가 가득 차면 span을 드롭한다. collector가 죽어도 요청
        # 처리 스레드는 절대 블로킹되지 않는다.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

        _set_w3c_propagator()
    except Exception:
        logger.error(
            "OpenTelemetry tracing setup failed; continuing without tracing",
            exc_info=True,
        )
        return False

    _instrument_libraries()

    _tracing_configured = True
    logger.info(
        "OpenTelemetry tracing enabled (service.name=%s)", service_name()
    )
    return True


def _instrument_libraries() -> None:
    """
    app 객체가 필요 없는 라이브러리 instrumentation을 적용한다.

    대상은 **애플리케이션 코드가 실제로 호출하는 라이브러리로만**
    한정한다.
    - botocore: app/core/cognito.py의 boto3 cognito-idp client.
      boto3는 내부적으로 botocore를 쓰므로 공식 instrumentation은
      botocore용 하나뿐이다(InitiateAuth/SignUp/GetUser/RevokeToken
      /ChangePassword 등 모든 Cognito 호출이 여기서 span이 된다).
    - sqlalchemy: app/core/database.py의 단일 엔진(PostgreSQL).
    - urllib: PyJWKClient(app/core/cognito.py)가 Cognito JWKS를
      가져올 때 표준 라이브러리 urllib을 사용한다. 토큰 검증
      지연의 실제 원인이 되는 구간이라 추적 가치가 있다.

    httpx는 의도적으로 제외한다. 현재 애플리케이션 코드에는 outbound
    httpx 호출이 없고(httpx는 fastapi.testclient가 쓰는 테스트 전용
    의존성이다), 쓰지 않는 라이브러리를 미리 계측해두면 런타임에
    패치되는 코드와 유지해야 할 의존성만 늘어난다. 다른 MSA를
    호출하는 코드를 추가할 때 그 변경과 함께
    opentelemetry-instrumentation-httpx를 requirements.txt에 넣고
    여기에 _httpx()를 추가하면 된다 — trace context 전파는 그 시점에
    자동으로 따라온다.
    """
    from app.core.database import engine

    def _botocore():
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        BotocoreInstrumentor().instrument()

    def _sqlalchemy():
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)

    def _urllib():
        from opentelemetry.instrumentation.urllib import URLLibInstrumentor

        URLLibInstrumentor().instrument()

    _instrument("botocore", _botocore)
    _instrument("sqlalchemy", _sqlalchemy)
    _instrument("urllib", _urllib)


def instrument_app(app) -> None:
    """
    FastAPI(ASGI) inbound HTTP instrumentation을 적용한다.

    inbound 요청의 traceparent 헤더를 읽어 상위 trace를 이어받는
    지점이 바로 여기다. tracing이 비활성이면 아무것도 하지 않는다
    (테스트/로컬에서 불필요한 미들웨어가 끼지 않는다).
    """
    if not _tracing_configured:
        return

    def _fastapi():
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=os.getenv(
                "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", _DEFAULT_EXCLUDED_URLS
            ),
        )

    _instrument("fastapi", _fastapi)


__all__ = ["configure_tracing", "instrument_app", "tracing_enabled"]
