"""
Prometheus HTTP 메트릭 (app/core/metrics.py, app/api/metrics.py).

검증 범위:
- /metrics 가 Micrometer 호환 시계열(http_server_requests_seconds_*)을
  노출하는지 — 인프라의 "HTTP 5xx" / "p99" 알림이 이 이름에 의존한다
- application 라벨이 로그/트레이스의 service 이름과 같은 값인지
  (RCA Agent 의 메트릭<->로그<->트레이스 상관분석 전제)
- uri 라벨이 실제 경로가 아니라 라우트 템플릿인지(카디널리티)
- status/outcome 이 계열에 맞게 붙는지
- probe/스크레이핑 경로(/health, /metrics)는 집계에서 빠지는지
"""

from prometheus_client import REGISTRY
from fastapi.testclient import TestClient

from app.core.metrics import _outcome
from app.main import app

client = TestClient(app)

METRIC = "http_server_requests_seconds"
COUNT = f"{METRIC}_count"
BUCKET = f"{METRIC}_bucket"


def _count(**labels) -> float:
    labels.setdefault("application", "backend-auth")
    return REGISTRY.get_sample_value(COUNT, labels) or 0.0


class TestExposition:
    def test_metrics_endpoint_is_reachable(self):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_exposes_micrometer_compatible_series(self):
        client.get("/mock")
        body = client.get("/metrics").text
        # 인프라 알림 규칙이 참조하는 정확한 시계열 이름.
        assert COUNT in body
        assert BUCKET in body
        assert f"{METRIC}_sum" in body

    def test_application_label_matches_service_name(self):
        client.get("/mock")
        body = client.get("/metrics").text
        assert 'application="backend-auth"' in body


class TestLabels:
    def test_successful_request_is_counted_by_route_template(self):
        before = _count(method="GET", uri="/mock", status="200", outcome="SUCCESS")
        client.get("/mock")
        after = _count(method="GET", uri="/mock", status="200", outcome="SUCCESS")
        assert after == before + 1

    def test_uri_label_is_the_route_template_not_the_raw_path(self):
        """path 파라미터가 있는 경로도 템플릿 하나로 접혀야 한다."""
        # /api/v1/terms/{...} 형태가 없더라도, 존재하지 않는 경로는
        # 전부 NOT_FOUND 하나로 접힌다(스캐너 카디널리티 방지).
        before = _count(method="GET", uri="NOT_FOUND", status="404", outcome="CLIENT_ERROR")
        client.get("/this-route-does-not-exist-12345")
        after = _count(method="GET", uri="NOT_FOUND", status="404", outcome="CLIENT_ERROR")
        assert after == before + 1

    def test_client_error_outcome(self):
        # /mock 은 GET 전용 -> POST 는 405. 경로 자체는 매칭되므로
        # uri 는 라우트 템플릿("/mock")으로 집계된다.
        before = _count(method="POST", uri="/mock", status="405", outcome="CLIENT_ERROR")
        client.post("/mock")
        after = _count(method="POST", uri="/mock", status="405", outcome="CLIENT_ERROR")
        assert after == before + 1

    def test_outcome_mapping(self):
        assert _outcome(200) == "SUCCESS"
        assert _outcome(301) == "REDIRECTION"
        assert _outcome(404) == "CLIENT_ERROR"
        assert _outcome(500) == "SERVER_ERROR"
        assert _outcome(503) == "SERVER_ERROR"


class TestProbePathsExcluded:
    def test_health_is_not_counted(self):
        before = _count(method="GET", uri="/health", status="200", outcome="SUCCESS")
        client.get("/health")
        after = _count(method="GET", uri="/health", status="200", outcome="SUCCESS")
        assert after == before  # /health 는 집계 대상이 아니다

    def test_metrics_scrape_is_not_counted(self):
        client.get("/metrics")
        body = client.get("/metrics").text
        assert 'uri="/metrics"' not in body


class TestRequestsStillServed:
    def test_normal_response_body_is_unchanged(self):
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/mock").json()["message"] == "pong"
