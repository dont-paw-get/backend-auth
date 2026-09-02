from fastapi import APIRouter, Response

from app.core.metrics import metrics_exposition

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    """
    Prometheus 스크레이핑 엔드포인트.

    dev overlay 의 ServiceMonitor(`k8s/overlays/dev/servicemonitor.yaml`)가
    이 경로를 30초 간격으로 긁어 간다. 클러스터 내부(Prometheus)에서만
    호출되며, 응답에는 사용자 데이터가 없다 — 라우트별 요청 수/지연
    히스토그램뿐이다.
    """
    body, content_type = metrics_exposition()
    return Response(content=body, media_type=content_type)
