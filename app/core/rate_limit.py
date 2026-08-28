"""
인증 API용 최소 인메모리 슬라이딩 윈도우 rate limiter
(CLIAR-160, Phase 6, PLAN.md §9.1).

PLAN.md §9.1의 1차 범위 그대로: 외부 dependency 없이 프로세스 메모리에
카운터를 두고, FastAPI dependency로 개별 endpoint에 부착한다.

한계(멀티 파드): 이 리미터는 프로세스(파드) 단위로만 카운트한다.
prod가 여러 replica로 뜨면 각 파드가 독립된 카운터를 가지므로 실효
한도가 replica 수만큼 늘어나며, 이는 정확한 전역(global) rate limit이
아니다. 분산 환경에서 정확한 전역 제한이 필요하면 Redis 백엔드나 AWS
WAF rate-based rule로 이관해야 한다(PLAN.md §9.1 2차 범위, §15
범위 외). 이 파일은 그 이관 작업을 포함하지 않는다.

키 구성: `f"{endpoint}:{client_ip}"`만 사용한다. password/token/
confirmation code 등 민감정보는 절대 key에 포함하지 않는다 —
인증 시도의 성공/실패 여부와 무관하게 같은 (endpoint, IP) 조합은
항상 동일한 키로 카운트되므로, 결과와 상관없이 반복 요청 자체를
제한한다.
"""

import re
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

_RATE_SPEC_RE = re.compile(r"^(?P<count>\d+)/(?P<unit>second|minute|hour)$")

_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}

RATE_LIMIT_EXCEEDED_DETAIL = "잠시 후 다시 시도해주세요"


class RateLimitRule:
    """
    "10/minute" 같은 설정 문자열을 (허용 횟수, 윈도우 초) 쌍으로
    해석한다. app/core/cognito_errors.py의 TooManyRequestsException
    매핑과 동일한 사용자 문구(§6)를 쓰기 위해 detail 자체는 여기서
    갖지 않고 호출자가 RATE_LIMIT_EXCEEDED_DETAIL을 사용한다.
    """

    __slots__ = ("max_requests", "window_seconds", "spec")

    def __init__(self, spec: str):
        match = _RATE_SPEC_RE.match(spec)
        if not match:
            raise ValueError(
                f"Invalid rate limit spec {spec!r}; expected '<count>/<second|minute|hour>'"
            )
        self.max_requests = int(match.group("count"))
        self.window_seconds = _UNIT_SECONDS[match.group("unit")]
        self.spec = spec


class SlidingWindowRateLimiter:
    """
    단일 프로세스 안에서 (key -> 최근 요청 시각 목록)을 유지하는
    로그 기반 슬라이딩 윈도우 리미터.

    스레드 안전성: uvicorn의 sync FastAPI 엔드포인트는 스레드풀에서
    실행되므로 lock으로 보호한다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, rule: RateLimitRule) -> bool:
        """
        허용되면 True를 반환하고 이번 요청을 기록한다. 한도를
        초과했으면 기록하지 않고 False를 반환한다.
        """
        now = time.monotonic()
        window_start = now - rule.window_seconds

        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < window_start:
                hits.popleft()

            if len(hits) >= rule.max_requests:
                return False

            hits.append(now)
            return True

    def reset(self) -> None:
        """
        모든 카운터를 초기화한다. 프로덕션 코드 경로에서는 쓰이지
        않고, 테스트 간 상태 오염을 막기 위한 용도로만 사용한다
        (tests/conftest.py의 autouse fixture 참고).
        """
        with self._lock:
            self._hits.clear()


_limiter = SlidingWindowRateLimiter()


def reset_rate_limits() -> None:
    """전역 리미터 상태를 초기화한다(테스트 전용 진입점)."""
    _limiter.reset()


# 신뢰하는 프록시 홉 수 (CLIAR-160 보안 리뷰로 leftmost에서 수정).
#
# 현재 배포 구조(k8s/base/ingress.yaml, k8s/cluster/ingressclass-alb.yaml
# 읽기 전용으로 확인, 이번 티켓에서 k8s manifest 자체는 변경하지
# 않음):
#   인터넷 client --(단일 hop)--> AWS ALB(internet-facing,
#   target-type: ip) --(pod network, kube-proxy/NodePort 경유 없음)-->
#   이 Pod
# CloudFront/WAF 등 ALB 앞단의 추가 프록시는 manifest 어디에도 없다
# (k8s/, argocd/ 전체 검색 결과 없음). 즉 backend-auth 입장에서
# "신뢰 가능한" 프록시 hop은 ALB 단 하나뿐이다.
#
# AWS ALB는 X-Forwarded-For 헤더가 이미 있어도 그 값을 지우거나
# 재정렬하지 않고, 자신이 실제로 TCP 연결을 맺은 client의 IP를
# **항상 헤더 맨 뒤(rightmost)에 append**한다(AWS 공식 문서: "If the
# request already contains this header, the load balancer appends
# the client's IP address to the end of the existing header"). 즉
# client가 요청에 X-Forwarded-For를 아무리 조작해서 실어 보내도
# (예: 매 요청마다 값을 바꿔가며 rate limit 우회 시도), ALB가 맨
# 뒤에 추가하는 값만은 위조할 수 없다.
#
# 반대로 기존 구현처럼 leftmost(첫 번째 값)를 쓰면, 그 값은 client가
# 헤더에 직접 써서 보낸 임의의 문자열이므로 100% 신뢰할 수 없다 —
# client가 매 요청마다 다른 leftmost 값을 보내면 limiter가 매번 다른
# client로 오인해 rate limit이 사실상 무력화된다.
#
# 이 값을 바꿔야 하는 경우: ALB 앞단에 CloudFront/WAF 등 신뢰 가능한
# 프록시가 추가되면, 그 hop 수만큼 늘려야 한다(예: ALB 앞에
# CloudFront가 추가되면 2). 이번 티켓은 k8s manifest를 변경하지
# 않으므로 현재 확인된 구조(hop=1)만 반영한다.
_TRUSTED_PROXY_HOPS = 1


def _client_identifier(request: Request) -> str:
    """
    rate limit 키에 쓸 client 식별자를 얻는다. 비밀번호/토큰 등
    민감정보는 절대 쓰지 않고 IP만 사용한다(PLAN.md §9.1).

    X-Forwarded-For가 있으면 신뢰 가능한 hop 수(_TRUSTED_PROXY_HOPS)
    만큼 오른쪽에서부터 센 값을 사용한다(hop=1이면 마지막 값
    = ALB가 append한 실제 client IP). 값 개수가 신뢰 hop 수보다
    적으면(예: ALB를 거치지 않은 직접 접근에서 client가 헤더를
    위조해 하나만 채워 보낸 경우) 그 헤더를 신뢰하지 않고 TCP peer로
    폴백한다 — 이 경우도 "가장 왼쪽 값을 그대로 신뢰"하지 않는다.

    헤더 자체가 없으면(로컬 개발, ALB를 거치지 않는 테스트) TCP
    연결의 peer address로 폴백한다(애플리케이션 계층에서 위조할 수
    없는 값이므로 안전하다).
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= _TRUSTED_PROXY_HOPS:
            return parts[-_TRUSTED_PROXY_HOPS]

    client = request.client
    return client.host if client is not None else "unknown"


def rate_limit(endpoint: str, spec: str):
    """
    FastAPI dependency factory.

    `endpoint`는 라우트 경로가 아니라 호출부가 명시하는 고정
    식별자다(예: "login"). 카운터를 endpoint별로 분리하기 위한
    namespace 역할만 하며, 실제 요청 경로 문자열에 의존하지 않으므로
    라우트 등록 방식이 바뀌어도 안정적이다.

    `spec`은 "10/minute" 같은 문자열이며, 이 함수가 호출되는 시점
    (모듈 import 시, 즉 endpoint 데코레이터 평가 시점)에 1회
    파싱한다.
    """
    rule = RateLimitRule(spec)

    def dependency(request: Request) -> None:
        key = f"{endpoint}:{_client_identifier(request)}"
        if not _limiter.allow(key, rule):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=RATE_LIMIT_EXCEEDED_DETAIL,
            )

    return dependency
