"""
Prometheus HTTP 메트릭 노출 (관측 스택 연동: /metrics -> ServiceMonitor
-> Prometheus -> Grafana / RCA Agent).

이 서비스는 Spring 이 아니라 FastAPI 다. 하지만 인프라(`dpgy-infra`)의
알림 규칙("HTTP 5xx 에러율", "p99 레이턴시")은 Spring Boot Actuator +
Micrometer 가 내보내는 `http_server_requests_seconds_*` 시계열을
전제로 작성돼 있다. 그래서 여기서도 **같은 메트릭 이름과 같은 라벨
구조**로 노출한다 — 인프라가 이 서비스만을 위한 별도 쿼리를 만들지
않아도 되도록.

Micrometer 의 `http.server.requests` Timer 는 Prometheus 포맷에서
아래 세 시계열로 나온다.

    http_server_requests_seconds_bucket{le="..."}   (히스토그램 버킷)
    http_server_requests_seconds_sum
    http_server_requests_seconds_count

`prometheus_client.Histogram` 을 `http_server_requests_seconds` 라는
이름으로 만들면 정확히 이 세 가지가 생성된다. 버킷이 있어야
`histogram_quantile()` 로 p99 를 계산할 수 있고, `_count` 에 `status`
라벨이 있어야 5xx 비율을 낼 수 있다.

라벨 (Micrometer 기본 태그와 정렬)
--------------------------------
- `application` : 서비스 이름. 로그의 `service`, 트레이스의
  `service.name` 과 **같은 값**이어야 RCA Agent 가 메트릭 <-> 로그
  <-> 트레이스를 같은 서비스로 상관분석한다. 세 곳 모두
  `OTEL_SERVICE_NAME`(기본 "backend-auth")을 공유한다.
- `method` : HTTP 메서드.
- `uri` : **라우트 템플릿**(`/api/v1/auth/login`). 실제 경로가 아니라
  매칭된 라우트의 path 를 쓴다 — path 파라미터가 들어간 실제 URL 을
  라벨로 쓰면 카디널리티가 폭증한다. 매칭되는 라우트가 없으면
  (스캐너 등) `NOT_FOUND` 하나로 접는다.
- `status` : HTTP 상태 코드(문자열).
- `outcome` : status 계열(`SUCCESS` / `CLIENT_ERROR` / `SERVER_ERROR`
  ...). Micrometer 의 `outcome` 태그와 같은 값.

`exception` 태그는 넣지 않는다 — 5xx/p99 알림에 필요 없고, 예외
클래스명이 라벨이 되면 카디널리티만 늘어난다.

probe 경로 제외
--------------
`/health`(kubelet probe)와 `/metrics`(이 엔드포인트 자신, Prometheus
스크레이핑) 요청은 메트릭에 집계하지 않는다. 트레이스에서 같은
경로를 제외하는 것과 같은 이유다(`app/core/tracing.py`): 주기적이고
동일하며 실제 트래픽 신호를 희석시킨다. 알림이 보는 것은 사용자
요청의 에러율과 지연이다.

관측 실패가 인증 API 를 깨뜨리지 않는다
------------------------------------
미들웨어는 요청 처리 전후로 시간을 재고 카운터를 올릴 뿐이며,
집계 중 예외가 나도 요청 응답에는 영향을 주지 않는다. `/metrics`
직렬화가 실패해도 500 을 반환할 뿐 애플리케이션은 계속 동작한다.
"""

import time

from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest

from app.core.logging_config import service_name

# 메트릭에 집계하지 않는 경로. probe / 스크레이핑 요청은 실제 트래픽이
# 아니다. app/core/tracing.py 의 excluded_urls 와 목적이 같다.
_EXCLUDED_PATHS = frozenset({"/health", "/metrics"})

# Micrometer `http.server.requests` Timer 와 같은 이름 -> 같은 파생
# 시계열(_bucket / _sum / _count). 버킷은 prometheus_client 기본값
# (5ms ~ 10s, 초 단위)을 그대로 쓴다 — 인증 API 지연 범위를 충분히
# 덮고, p99 를 histogram_quantile 로 계산할 수 있다.
HTTP_SERVER_REQUESTS = Histogram(
    "http_server_requests_seconds",
    "HTTP server request latency and count (Micrometer-compatible)",
    labelnames=("application", "method", "uri", "status", "outcome"),
)


def _outcome(status_code: int) -> str:
    """HTTP 상태 코드 -> Micrometer `outcome` 태그 값."""
    if 100 <= status_code < 200:
        return "INFORMATIONAL"
    if 200 <= status_code < 300:
        return "SUCCESS"
    if 300 <= status_code < 400:
        return "REDIRECTION"
    if 400 <= status_code < 500:
        return "CLIENT_ERROR"
    return "SERVER_ERROR"


def _route_template(scope) -> str:
    """
    매칭된 라우트의 path 템플릿을 반환한다.

    Starlette 라우터는 매칭에 성공하면 `scope["route"]` 에 Route 객체를
    넣는다(dict 를 in-place 로 수정하므로 미들웨어가 제어를 돌려받은
    시점에 조회 가능하다). 매칭되는 라우트가 없으면 — 존재하지 않는
    경로, 스캐너 트래픽 — `NOT_FOUND` 하나로 접어 카디널리티를 막는다.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path or "NOT_FOUND"


def _record(scope, status_code: int, started_at: float) -> None:
    HTTP_SERVER_REQUESTS.labels(
        application=service_name(),
        method=scope.get("method", "UNKNOWN"),
        uri=_route_template(scope),
        status=str(status_code),
        outcome=_outcome(status_code),
    ).observe(time.perf_counter() - started_at)


class PrometheusMiddleware:
    """
    HTTP 요청마다 `http_server_requests_seconds` 히스토그램을 갱신하는
    순수 ASGI 미들웨어.

    `BaseHTTPMiddleware`(=`@app.middleware("http")`) 대신 순수 ASGI 로
    구현한 이유: (1) 스트리밍 응답/백그라운드 태스크와의 알려진
    상호작용 문제를 피하고, (2) 제어를 돌려받은 뒤 `scope["route"]` 를
    안정적으로 읽어 라우트 템플릿을 라벨로 쓰기 위해서다.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in _EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 핸들러가 예외를 던졌다 = 아직 응답 시작 전 -> 500 으로
            # 집계하고 예외는 그대로 전파한다(에러 응답 생성은 상위
            # ServerErrorMiddleware 의 몫).
            _record(scope, 500, started_at)
            raise

        _record(scope, status_holder["code"], started_at)


def metrics_exposition() -> tuple[bytes, str]:
    """(본문, Content-Type) 튜플. `/metrics` 핸들러가 그대로 반환한다."""
    return generate_latest(), CONTENT_TYPE_LATEST


__all__ = ["PrometheusMiddleware", "metrics_exposition", "HTTP_SERVER_REQUESTS"]
