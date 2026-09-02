# 관측 (구조화 로깅 / 분산 추적)

backend-auth 가 내보내는 텔레메트리의 형식·설정·보안 정책을 정리한다.
수집·저장·조회 스택(Alloy / Loki / OpenTelemetry Collector / Tempo)은 이
저장소가 아니라 `dpgy-infra` 가 소유한다
(`docs/production_architecture.md` §9).

```
로그   Application stdout ──► Grafana Alloy ──► Loki
트레이스 Application ──OTLP/HTTP──► OpenTelemetry Collector ──► Grafana Tempo
```

두 신호는 `service.name = backend-auth` 와 `trace_id` 로 서로 연결된다.
Loki 에서 찾은 로그 한 줄의 `trace_id` 를 Tempo 에 넣으면 그 요청의 전체
trace 가 나오고, 반대 방향도 같다.

관련 파일

| 파일 | 역할 |
|---|---|
| `app/core/logging_config.py` | stdout JSON 포매터, 민감정보 마스킹, uvicorn 로거 통합 |
| `app/core/tracing.py` | TracerProvider, OTLP exporter, 라이브러리 instrumentation |
| `app/core/audit_log.py` | 인증 보안 감사 이벤트 (로그 + span 속성) |
| `app/main.py` | 기동 시 두 모듈 호출 |
| `tests/test_observability.py` | 위 동작과 마스킹에 대한 테스트 |

---

## 1. 로깅

### 출력 형식

한 줄에 JSON 객체 하나. 최소 필드는 항상 존재한다.

```json
{
  "timestamp": "2026-09-01T02:14:07.881Z",
  "level": "INFO",
  "service": "backend-auth",
  "logger": "app.audit",
  "message": "auth_audit event=login outcome=success member_id=1111...",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "6d330cfa10ff0162",
  "event": "login",
  "outcome": "success",
  "member_id": "11111111-2222-3333-4444-555555555555"
}
```

- `timestamp` — UTC ISO-8601, 밀리초까지.
- `trace_id` / `span_id` — 현재 활성 span 에서 가져온다. 요청 컨텍스트
  밖(기동 로그 등)이거나 tracing 이 꺼져 있으면 `null`.
- 그 밖의 필드는 호출부가 `extra=` 로 넘긴 값이며 top-level 로 펼쳐진다
  (LogQL 에서 중첩 없이 바로 필터할 수 있게).

### 기존 구조와의 관계

애플리케이션 코드는 그대로다. 모든 모듈이 이미 표준
`logging.getLogger(__name__)` 를 쓰고 있고, 이 작업은 그 위에 핸들러와
포매터만 얹었다. `logger.info(...)` 호출부를 고치지 않았다.

> 부수 효과 하나: 이전에는 root logger 에 핸들러가 아예 없어서
> `logging.lastResort` 가 WARNING 이상만 stderr 로 내보내고 있었다. 즉
> `app/core/audit_log.py` 의 INFO 감사 로그는 배포 환경에서 **출력되지
> 않고 있었다.** 이제 처음으로 수집된다.

uvicorn 은 애플리케이션 모듈을 import 하기 전에 자기 로거에 평문
핸들러를 달아둔다. `configure_logging()` 이 그 뒤에 실행되면서 해당
핸들러를 걷어내고 root 로 흘려보내므로, access 로그까지 같은 JSON
스트림으로 나온다(그리고 `trace_id` 도 함께 붙는다).

### 설정

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | root logger 레벨 |
| `LOG_FORMAT` | `json` | `json` \| `text` (로컬 개발용) |
| `OTEL_SERVICE_NAME` | `backend-auth` | `service` 필드 값 (트레이스와 공유) |

`LOG_FORMAT=text` 로 두어도 민감정보 마스킹은 동일하게 적용된다.

---

## 2. 분산 추적

### 활성화 조건

`OTEL_EXPORTER_OTLP_ENDPOINT`(또는 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`)
가 주입된 환경에서만 켜진다. 주소를 모르는 환경에서 굳이 켜서 연결 실패
로그를 반복해 남기지 않기 위한 의도된 동작이며, 로컬 개발과 pytest 가
자동으로 이 경로를 탄다. `OTEL_SDK_DISABLED=true` 로 강제로 끌 수도 있다.

**endpoint 는 코드 어디에도 하드코딩되어 있지 않다.**

현재 주입 현황:

| 환경 | `OTEL_EXPORTER_OTLP_ENDPOINT` | 샘플링 (`OTEL_TRACES_SAMPLER` / `_ARG`) |
|---|---|---|
| dev | `http://otel-collector.monitoring.svc.cluster.local:4318` | `parentbased_traceidratio` / `1.0` (전량) |
| prod | 미주입 — collector 주소 확인 후 `k8s/overlays/prod/configmap-patch.yaml` 에 추가 | (주입 시 결정) |

`app/core/tracing.py` 는 `TracerProvider` 에 sampler 를 넘기지 않는다 —
SDK 가 표준 환경변수 `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` 를
읽어 구성한다(기본값 `parentbased_always_on`). dev 는 트레이스 검증이
목적이라 `parentbased_traceidratio` + `ARG=1.0`(=100%)으로 둔다. ratio
방식이라 prod 로 확장할 때 `_ARG` 만 낮추면 코드 변경 없이 샘플링율이
바뀐다. `parentbased_*` 는 상위 `traceparent` 의 sampled 결정을 존중한다.

### 환경변수

```
OTEL_SERVICE_NAME=backend-auth
OTEL_EXPORTER_OTLP_ENDPOINT=<collector endpoint>       # 예: http://otel-collector.observability.svc.cluster.local:4318
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<environment>
```

exporter 는 **OTLP http/protobuf** 다. collector 의 otlp receiver 에서
HTTP endpoint(기본 **4318**)가 열려 있어야 한다. gRPC 포트(4317)를
가리키면 export 가 실패한다.

gRPC 대신 HTTP 를 고른 근거는 두 가지다.

1. **서비스 간 프로토콜 통일.** 여러 MSA 가 같은 collector 로 보내는
   구조에서 서비스마다 gRPC/HTTP 가 섞이면 collector receiver 설정,
   NetworkPolicy, 장애 시 확인 절차가 서비스마다 갈라진다. 플랫폼 전체가
   하나의 OTLP 전송 방식(http/protobuf, 4318)을 쓰면 그 설정과 운영
   절차를 한 벌로 유지할 수 있다. **다른 backend 서비스도 같은 선택을
   따르는 것을 전제로 한다.**
2. **런타임 의존성 단순화.** gRPC exporter 는 `grpcio` 라는 네이티브
   확장 의존성을 런타임에 추가로 끌어들인다. http/protobuf 는 순수
   파이썬 + protobuf 만 요구하므로 인증 서비스의 런타임 의존성 표면과
   이미지 크기를 불필요하게 늘리지 않는다.

`service.instance.id` 는 SDK 가 자동으로 넣으므로 prod 의 replica 2 개는
같은 `service.name` 안에서 구분된다.

### instrumentation 목록

전부 공식 OpenTelemetry Python instrumentation 이며, **애플리케이션
코드가 실제로 호출하는 라이브러리만** 켠다.

| 대상 | 패키지 | 계측되는 지점 |
|---|---|---|
| FastAPI / ASGI inbound | `...-instrumentation-fastapi` | 모든 HTTP 요청. inbound `traceparent` 를 이어받는 지점 |
| boto3 (Cognito) | `...-instrumentation-botocore` | InitiateAuth, SignUp, ConfirmSignUp, GetUser, RevokeToken, ChangePassword, ForgotPassword, AdminGetUser/AdminDeleteUser, DeleteUser |
| SQLAlchemy / PostgreSQL | `...-instrumentation-sqlalchemy` | `app/core/database.py` 의 엔진을 통한 모든 쿼리 |
| urllib | `...-instrumentation-urllib` | `PyJWKClient` 의 Cognito JWKS 조회 (토큰 검증 지연의 실제 원인 구간) |

boto3 전용 instrumentation 은 없다 — boto3 는 내부적으로 botocore 를
쓰므로 공식 지원은 botocore 하나뿐이고, 그것으로 모든 Cognito 호출이
span 이 된다.

**httpx 는 계측하지 않는다.** 이 서비스의 애플리케이션 코드에는 outbound
httpx 호출이 없고, httpx 는 `fastapi.testclient` 가 쓰는 테스트 전용
의존성이다. 쓰지 않는 라이브러리를 미리 계측해두면 런타임에 패치되는
코드와 유지해야 할 의존성만 늘어난다. 다른 MSA 를 호출하는 코드를
추가할 때 **그 변경과 함께**

- `requirements.txt` 에 `opentelemetry-instrumentation-httpx` 추가
- `app/core/tracing.py` 의 `_instrument_libraries()` 에 `_httpx()` 추가

를 하면 된다. trace context 전파는 그 시점에 자동으로 따라온다.
`tests/test_observability.py::TestInstrumentationScope` 가 이 전제를
지킨다 — `app/` 에 httpx import 가 생기면 테스트가 실패하며 계측을 함께
추가하라고 알려준다.

### 중복 SERVER span 방지

FastAPI instrumentation 은 내부적으로 ASGI `OpenTelemetryMiddleware` 를
사용한다. 따라서 **`FastAPIInstrumentor` 와 `OpenTelemetryMiddleware` 를
함께 적용하면 같은 요청에 SERVER span 이 두 개 생긴다.** 이 저장소는
`FastAPIInstrumentor.instrument_app(app)` 하나만 쓰고
`OpenTelemetryMiddleware` 를 직접 `add_middleware` 하지 않는다
(`app/main.py` 의 `add_middleware` 는 CORS 하나뿐이다).

`instrument_app()` 은 `_is_instrumented_by_opentelemetry` 플래그로
보호되므로 두 번 호출해도 경고만 남기고 두 번째는 무시된다. 실제 export
된 span 으로 "요청 1건 = SERVER span 1개"를 확인했다
(`tests/test_observability.py::TestNoDuplicateServerSpan`).

> **운영 주의**: 컨테이너 CMD 를 `opentelemetry-instrument uvicorn ...`
> 으로 바꾸면 auto-instrumentation 이 `FastAPIInstrumentor().instrument()`
> 를 전역 적용해 우리 `instrument_app()` 과 겹친다. 이 저장소는 계측을
> 코드에서 명시적으로 하므로 **CMD 에 `opentelemetry-instrument` 를
> 붙이지 않는다**(`Dockerfile` 의 CMD 는 평범한 `uvicorn` 이다).

### Kubernetes probe endpoint 제외 정책

**`/health` 등 kubelet probe endpoint 는 trace 에서 제외한다.**
`app/core/tracing.py` 가 `excluded_urls` 기본값 `"health"` 로 적용하며,
`OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` 환경변수로 덮어쓸 수 있다.

근거:

- 이 저장소의 probe 주기는 readiness 10 초 + liveness 20 초다
  (`k8s/base/deployment.yaml`). 파드 하나당 하루 약 12,900 건
  (8,640 + 4,320)의 span 이 되고, prod replica 2 개면 그 두 배다.
- 그 span 은 전부 동일하고 애플리케이션 동작에 대해 알려주는 것이 없다.
  probe 실패는 이미 kubelet 이벤트와 파드 상태로 드러난다.
- Tempo 저장 비용과 collector 처리량을 실제 트래픽에 쓰기 위함이다.

**이 정책은 backend-auth 전용이 아니라 플랫폼 공통 규약으로 둔다.**
다른 backend 서비스도 probe endpoint 를 trace 에서 제외한다. 적용
지점은 둘 중 하나이고, 서비스 쪽에서 거르는 편이 collector 부하까지
줄이므로 우선한다.

| 적용 지점 | 방법 |
|---|---|
| 서비스 (우선) | 언어별 SDK 의 URL 제외 옵션. Python/OTel 은 `excluded_urls` 인자 또는 `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` / `OTEL_PYTHON_EXCLUDED_URLS` 환경변수 (이 저장소가 쓰는 방식). 다른 언어는 해당 SDK 의 동등 옵션을 쓴다 |
| collector (보완) | `filter` processor 로 probe 경로 span 을 일괄 드롭. SDK 제외 옵션이 없는 서비스나, 규약을 아직 적용하지 않은 서비스를 덮는 안전망 |

probe 경로가 `/health` 가 아닌 서비스는 그 서비스의 제외 설정에 자기
경로를 넣는다. Python 쪽 값은 정규식 부분일치 목록(쉼표 구분)이다.

애플리케이션 **로그**는 이 제외 정책의 대상이 아니다 — uvicorn access
로그에는 probe 요청이 그대로 남는다. 로그 볼륨이 문제가 되면 Alloy
단계에서 걸러내는 편이 낫다(앱이 자기 접근 로그를 감추는 것보다
수집 파이프라인에서 버리는 쪽이 되돌리기 쉽다).

### W3C trace context

전역 propagator 를 `tracecontext` + `baggage` 로 **명시적으로** 고정한다.
botocore instrumentation 이 의존성으로 AWS X-Ray propagator 를 함께
설치하기 때문에, 기본값에 기대지 않고 못박아 둔다.
`OTEL_PROPAGATORS` 가 주입되어 있으면 그 설정을 존중하고 건드리지 않는다.

- **inbound**: 다른 MSA 가 보낸 `traceparent` 헤더를 그대로 이어받아
  같은 trace 안의 자식 span 으로 붙는다.
- **outbound**: urllib / botocore 요청에 `traceparent` 를 주입한다.
  (httpx 를 쓰는 outbound 호출을 추가하면 위 "instrumentation 목록"의
  절차대로 계측을 함께 넣어야 전파된다.)

### custom span 을 추가하지 않은 이유

요구된 후보는 Cognito 인증 / 토큰 갱신 / 회원가입 세 구간이었다. 셋 다
**자동 instrumentation 으로 충분하다** — 각각이 endpoint 하나에
대응하므로, wrapper span 을 만들면 FastAPI 가 이미 만든 서버 span 과
시작·종료가 사실상 같은 span 이 하나 더 생길 뿐이다. 내부 단계는 이미
자식 span 으로 쪼개져 있다(botocore 가 Cognito 호출을, sqlalchemy 가 DB
조회를).

자동 instrumentation 이 주지 못하는 유일한 정보는 "이 요청이 도메인
관점에서 어떤 인증 이벤트였고 결과가 무엇이었나" 다. 그래서 span 을
늘리는 대신 `app/core/audit_log.py` 의 `audit()` 이 **현재 서버 span 에
속성만 덧붙인다**.

```
auth.event    = login | signup | signup_confirm | logout
                | password_forgot | password_reset | password_change
auth.outcome  = success | failure | revoke_failed | ...
auth.reason   = email_not_verified | cognito_error_401 | ...   (있을 때만)
```

덕분에 Tempo 에서 `auth.event="login" && auth.outcome="failure"` 로 실패한
로그인 trace 를 바로 찾을 수 있고, span 수는 늘지 않는다. 셋 다 고정된
열거형에 가까운 저카디널리티 값이라 사용자 식별에 쓸 수 없고 카디널리티
폭증도 없다.

span 에 올릴 수 있는 필드는 `app/core/audit_log.py` 의
`_SPAN_SAFE_FIELDS` **허용 목록**으로 관리한다(차단 목록이 아니다).
목록에 없는 필드는 로그에만 남으므로, 나중에 추가되는 식별자성 필드가
자동으로 trace 로 흘러드는 일이 없다.

### `member_id`(Cognito sub) 를 trace 에 넣지 않는 이유

Cognito sub 는 계정이 살아있는 동안 바뀌지 않는 **지속적 사용자
식별자**다. 가명 식별자이긴 하지만 `member` 테이블과 조인하면 곧바로
실제 사용자로 환원되므로 "내부 식별자라서 비민감"이라고 단정하지
않는다. 그래서 **어디에 남기는가** 를 용도별로 나눈다.

| | member_id 기록 | 근거 |
|---|---|---|
| 감사 로그 (`app.audit`) | **남긴다** | 이 로그의 존재 이유가 보안 감사다. "특정 계정에 대한 반복 인증 실패", "탈취 의심 계정의 활동 추적" 조사에는 DB 와 조인 가능한 식별자가 반드시 필요하다 |
| trace (span 속성) | **남기지 않는다** | trace 는 지연·오류를 보는 성능 신호이고 사용자 식별이 필요 없다 |

trace 에서 빼도 조사 능력은 줄지 않는다. 같은 요청의 감사 로그가 **동일한
`trace_id`** 를 갖고 있으므로, trace 에서 사용자를 알아야 하면 그
`trace_id` 로 Loki 를 조회하면 된다. 식별자 사본을 trace 에 한 벌 더 두는
것은 순수한 노출 확대이며, trace 는 성능 대시보드 성격상 감사 로그보다
열람 범위가 넓고 보존 정책도 다르기 때문에 더욱 그렇다.

**비식별화(해시)를 감사 로그에 적용하지 않은 이유**: 단방향 해시로
바꾸면 감사 로그를 `member` 테이블과 조인할 수 없어, 로그가 존재하는
목적 자체를 잃는다. 노출을 줄이는 수단으로는 "값을 훼손"하는 대신
"기록 위치를 감사 로그 하나로 제한"하는 편을 택했다. 감사 로그의 보존
기간·열람 권한을 좁히는 것은 앱이 아니라 Loki 쪽 정책으로 다룰 문제다.

### collector 장애 시 동작

인증 API 는 영향을 받지 않는다.

- export 는 `BatchSpanProcessor` 가 **백그라운드 스레드에서 비동기로**
  수행한다. 요청 처리 스레드는 collector 를 기다리지 않는다.
- 큐가 차면 span 을 드롭한다(요청을 막지 않는다).
- SDK 설정 자체가 실패해도 `configure_tracing()` 이 예외를 흡수하고
  `False` 를 반환한다 — 기동이 막히지 않는다.
- instrumentation 하나가 실패해도 그 계층의 span 만 포기하고 나머지는
  계속 적용한다.
- exporter 실패 로그는 ERROR 로만 남긴다(재시도마다 로그가 넘치지 않게
  `opentelemetry` 로거 레벨을 ERROR 로 올려둔다).

실제로 죽은 collector 주소를 넣고 전체 테스트를 돌려 확인했다 —
587 개 전부 통과한다.

---

## 3. 메트릭 (HTTP 요청 카운터 / 지연 히스토그램)

`app/core/metrics.py` + `app/api/metrics.py`.

이 서비스는 Spring 이 아니라 FastAPI 지만, 인프라(`dpgy-infra`)의 알림
규칙("HTTP 5xx 에러율", "p99 레이턴시")은 Spring Boot Actuator +
Micrometer 의 `http_server_requests_seconds_*` 시계열을 전제로 한다.
그래서 **같은 메트릭 이름 · 같은 라벨 구조**로 노출해 인프라가 이
서비스만을 위한 별도 쿼리를 만들지 않아도 되게 한다.

### 노출

```
로그    Application stdout ──► Grafana Alloy ──► Loki
트레이스 Application ──OTLP/HTTP──► OpenTelemetry Collector ──► Grafana Tempo
메트릭  Application /metrics ◄──scrape── Prometheus (ServiceMonitor)
```

`prometheus_client` 이 `GET /metrics` 로 Prometheus 텍스트 포맷을
내보낸다. **메트릭은 OTLP 로 보내지 않는다** — 인프라 Collector 는
traces 파이프라인만 받는다(`OTEL_METRICS_EXPORTER=none`).

`GET /metrics` 는 클러스터 내부(Prometheus)에서만 호출되며 응답에
사용자 데이터가 없다(라우트별 요청 수 / 지연 히스토그램뿐).

### 시계열

`prometheus_client.Histogram` 을 `http_server_requests_seconds` 라는
이름으로 만들면 Micrometer 의 `http.server.requests` Timer 와 동일하게
아래 셋이 나온다.

| 시계열 | 용도 |
|---|---|
| `http_server_requests_seconds_bucket{le="..."}` | `histogram_quantile()` 로 p99 계산 |
| `http_server_requests_seconds_count` | 요청 수 → 5xx 비율 |
| `http_server_requests_seconds_sum` | 평균 지연 |

버킷은 `prometheus_client` 기본값(5ms ~ 10s, 초 단위)이다.

### 라벨 (Micrometer 기본 태그와 정렬)

| 라벨 | 값 | 비고 |
|---|---|---|
| `application` | `backend-auth` | 로그 `service` / 트레이스 `service.name` 과 **같은 값** (`OTEL_SERVICE_NAME` 공유). RCA Agent 가 메트릭↔로그↔트레이스를 같은 서비스로 묶는 키 |
| `method` | `GET` / `POST` / ... | |
| `uri` | 라우트 **템플릿** (`/api/v1/auth/login`) | 실제 경로가 아님. 매칭 라우트가 없으면 `NOT_FOUND` 하나로 접음 (스캐너 카디널리티 방지) |
| `status` | `200` / `401` / `500` ... | 문자열 |
| `outcome` | `SUCCESS` / `CLIENT_ERROR` / `SERVER_ERROR` ... | Micrometer `outcome` 태그와 동일 |

`exception` 태그는 넣지 않는다 — 5xx/p99 알림에 불필요하고 카디널리티만
늘린다.

### probe 경로 제외

`/health`(kubelet probe)와 `/metrics`(스크레이핑 자신)는 메트릭
집계에서 뺀다. 트레이스에서 같은 경로를 빼는 것과 같은 이유다 —
주기적이고 동일하며 실제 트래픽 신호를 희석시킨다.

### 관측 실패가 인증 API 를 깨뜨리지 않는다

메트릭 미들웨어(`PrometheusMiddleware`, 순수 ASGI)는 요청 전후로 시간을
재고 카운터를 올릴 뿐이다. tracing 과 달리 외부 의존성이 없어 항상
켜져 있다. `/metrics` 직렬화가 실패해도 500 을 반환할 뿐 애플리케이션은
계속 동작한다.

### 확인 방법

```bash
# 파드에서 직접
kubectl -n dpyb-auth-dev exec deploy/backend-auth -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode())" \
  | grep http_server_requests_seconds_count

# Prometheus 가 스크레이핑 중인지 (target 이 UP 인지)
#   Prometheus UI > Status > Targets 에서 serviceMonitor/dpyb-auth-dev/backend-auth
```

---

## 4. 민감정보 보호

이 서비스는 인증 서비스다. 다음은 **어떤 경우에도** 로그·span 에 원문으로
남기지 않는다.

password / Authorization 헤더 / access token / refresh token / ID token /
JWT 전체 문자열 / Cognito client secret / SECRET_HASH / AWS credential /
session·cookie 값 / 사용자 개인정보 / request·response body

### 1차 방어선 — 애초에 넘기지 않는다

기존 코드가 이미 지켜온 정책이다. 실패 로그에도 `error_code` 와
`member_id`(Cognito sub) 만 남기고 Cognito 원문 메시지나 이메일은 남기지
않는다. `tests/test_audit_log.py` 가 이를 검증한다.

사용자 식별이 필요하면 **`member_id`(Cognito sub)만** 쓴다. 이메일은
마스킹 여부와 무관하게 기록하지 않는다. 아직 인증 전이라 `member_id` 를
모르는 시점(로그인 실패, signup, password forgot/reset)에는
`event`/`outcome`/`reason` 만 남긴다 — 이상 징후 탐지에 필요한 최소
정보다.

### 2차 방어선 — 내보내기 직전 마스킹

`logging_config.redact()` 가 stdout 으로 나가기 직전의 문자열에서
아래를 마스킹한다. 1차 방어선이 어딘가에서 깨졌을 때의 안전망이다.

| 패턴 | 처리 |
|---|---|
| JWT (`eyJ...` 3-파트) | `[REDACTED]` |
| `Bearer <token>` | `Bearer [REDACTED]` |
| AWS access key id (`AKIA`/`ASIA`...) | `[REDACTED]` |
| `password=` / `secret=` / `*_token=` / `cookie=` / `session=` 등 key=value | 값만 `[REDACTED]` |
| 이메일 주소 | `[REDACTED]@domain` (도메인만 남김) |

`extra=` 로 넘어온 필드 중 이름 자체가 민감한 것(`password`,
`access_token`, `email`, `code`, `cookie`, `aws_secret_access_key` …)은
마스킹이 아니라 **통째로 제거**한다.

`member_id` 는 마스킹하지 않는다 — 감사 로그에서 조사에 쓰이는 유일한
상관관계 키이기 때문이다(위 "`member_id` 를 trace 에 넣지 않는 이유"
참고). 대신 기록 위치를 감사 로그 하나로 제한한다.

### span 쪽 보호

- HTTP 헤더 캡처(`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`)를 켜지
  않는다 → Authorization / Cookie 헤더가 span 에 실리지 않는다.
- request/response body 는 어떤 instrumentation 도 수집하지 않는다.
- SQLAlchemy 는 SQL 문만 기록하고 bind parameter 는 기록하지 않는다
  (이메일 등 값이 들어가지 않는다).
- botocore 는 operation 이름 / region / request id 만 기록하고
  InitiateAuth 의 `AuthParameters`(password, SECRET_HASH, refresh token)는
  기록하지 않는다.
- `audit()` 이 span 에 붙이는 속성은 감사 로그와 같은 비민감 필드뿐이며,
  민감 키는 같은 필터로 걸러진다.

login → password change → password reset → logout 전체 흐름을 DEBUG
레벨로 돌리고 실제 OTLP 페이로드와 stdout 을 모두 검사해, 위 목록의 값이
하나도 나타나지 않음을 확인했다.

---

## 5. 예상 trace 예시

`POST /api/v1/auth/login` (다른 MSA 에서 `traceparent` 를 받은 경우)

```
trace 4bf92f3577b34da6a3ce929d0e0e4736
│
├─ [gateway/다른 MSA 의 span]                        (상위 서비스)
│
└─ SERVER  POST /api/v1/auth/login                   backend-auth   62ms
   │  http.method=POST  http.route=/api/v1/auth/login  http.status_code=200
   │  auth.event=login  auth.outcome=success
   │  (member_id 는 span 에 없다 — 같은 trace_id 의 감사 로그에 있다)
   │
   ├─ CLIENT  cognito-idp.InitiateAuth                botocore       38ms
   │            rpc.service=cognito-idp  aws.region=ap-northeast-2
   ├─ CLIENT  cognito-idp.GetUser                     botocore       14ms
   └─ CLIENT  SELECT dpyb_auth.member                 sqlalchemy      3ms
                db.system=postgresql
```

같은 요청의 로그(Loki):

```json
{"timestamp":"...","level":"INFO","service":"backend-auth","logger":"app.audit",
 "message":"auth_audit event=login outcome=success member_id=1111...",
 "trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"6d330cfa10ff0162",
 "event":"login","outcome":"success","member_id":"1111..."}
```

`Authorization` 헤더가 필요한 요청(예: `GET /users/me`)에서는 JWKS 조회가
캐시 미스일 때 urllib span 이 하나 더 붙는다.

```
└─ SERVER  GET /users/me                             backend-auth
   ├─ CLIENT  GET .../.well-known/jwks.json          urllib        (최초 1회)
   └─ CLIENT  SELECT dpyb_auth.member                sqlalchemy
```

실패한 로그인:

```
└─ SERVER  POST /api/v1/auth/login                   backend-auth
   │  http.status_code=401
   │  auth.event=login  auth.outcome=failure  auth.reason=cognito_error_401
   └─ CLIENT  cognito-idp.InitiateAuth  (error)      botocore
```

---

## 6. 인프라에 필요한 설정

이 저장소 밖(`dpgy-infra`)에서 해야 하는 일.

### 로그 (이미 동작함)

앱은 stdout 으로 JSON 을 내보내므로 **추가 작업이 없다.** Alloy 가 파드
stdout 을 수집하도록 되어 있으면 그대로 들어온다. Loki 쪽에서 확인할 것:

- JSON 파싱 단계(`json` stage)를 두어 `level` / `service` / `trace_id` 를
  라벨 또는 구조화 메타데이터로 승격.
- `trace_id` 를 Tempo 데이터소스에 연결하는 **derived field** 설정 —
  Grafana 로그 화면에서 trace 로 바로 점프할 수 있게 한다. 이게 로그↔트레이스
  상관관계의 실제 사용 지점이다.
- 라벨 카디널리티 주의: `trace_id` 는 라벨이 아니라 구조화 메타데이터로
  두어야 한다.

### 메트릭 (dev 는 이 저장소에서 완료)

이 저장소가 `k8s/overlays/dev/servicemonitor.yaml` 로 ServiceMonitor 를
배포한다. 인프라 Prometheus 는 `serviceMonitorSelectorNilUsesHelmValues=false`
라 네임스페이스·라벨 셀렉터 제약이 없으므로 별도 등록 작업이 없다.
인프라 쪽에서 확인/작업할 것:

- Prometheus UI > Status > Targets 에서
  `serviceMonitor/dpyb-auth-dev/backend-auth` 가 **UP** 인지.
- 알림 규칙이 `http_server_requests_seconds_bucket` /
  `_count` 를 그대로 쓰면 된다(Micrometer 와 이름 동일). 라벨은
  `application="backend-auth"`, `uri`(라우트 템플릿), `status`, `outcome`,
  `method`. `exception` 라벨은 없다.
- NetworkPolicy 가 있다면 `monitoring` → `dpyb-auth-dev` 로 들어오는
  8000/TCP 스크레이핑을 허용한다.

### 트레이스 (collector 주소 주입)

1. **OpenTelemetry Collector** 의 `otlp` receiver 에서 **HTTP(4318)** 를
   활성화한다. 앱은 http/protobuf 로 보낸다.
2. collector Service 의 DNS 를 확인해 configmap 에 주입한다.
   `k8s/overlays/{dev,prod}/configmap-patch.yaml` 의
   `OTEL_EXPORTER_OTLP_ENDPOINT` 에 실제 주소를 넣는다. 경로 없이
   `:4318` 까지만 쓴다 — exporter 가 `/v1/traces` 를 자동으로 붙인다.

   ```yaml
   OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector.monitoring.svc.cluster.local:4318"
   ```

   **dev 는 주입 완료.** prod 는 collector 주소 확인 후 추가한다. 이 값이
   없는 환경은 tracing 이 꺼진 채로 동작한다(로그는 정상).

   > 이 ConfigMap 은 `configMapGenerator` 가 아니라 이름 고정 리소스라
   > 해시 suffix 가 붙지 않는다. 즉 값만 바꿔서는 실행 중 파드가
   > 재기동되지 않는다. 다음 이미지 태그 bump(CI 가 develop 머지 시
   > 커밋)로 rollout 될 때 반영되며, 즉시 반영하려면
   > `kubectl -n dpyb-auth-dev rollout restart deploy/backend-auth`.
3. NetworkPolicy 가 있다면 `dpyb-auth-dev` / `dpyb-auth` 네임스페이스에서
   collector 네임스페이스(`monitoring`)로 나가는 4318 트래픽을 허용한다.
   (dev 는 OTLP HTTP 수신이 정상 동작함을 확인했다.)
4. collector 의 `otlp` exporter 를 Tempo 로 연결한다.
5. Grafana 에서 Tempo 데이터소스에 **trace-to-logs** 설정을 추가해
   `service.name` 과 `trace_id` 로 Loki 를 역참조하게 한다.

### 확인 방법

```bash
kubectl -n dpyb-auth-dev logs deploy/backend-auth | head -5     # JSON 인지
kubectl -n dpyb-auth-dev logs deploy/backend-auth | jq -r .level  # 파싱되는지

# 메트릭이 스크레이핑되는지 (파드에서 직접)
kubectl -n dpyb-auth-dev exec deploy/backend-auth -- \
  python -c "import urllib.request as u;print(u.urlopen('http://localhost:8000/metrics').read().decode())" \
  | grep http_server_requests_seconds_count

# collector 주입 후: traceparent 를 넣어 호출하고 그 trace_id 로 Tempo 조회
curl -X POST https://<host>/api/v1/auth/logout \
  -H "traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
```

`OTEL_EXPORTER_OTLP_ENDPOINT` 가 없을 때 기동 로그에 다음이 남는다 —
tracing 이 꺼진 이유를 이 줄로 확인할 수 있다.

```
OpenTelemetry tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT is not configured)
```

주입되어 정상 활성화되면 이렇게 남는다.

```
OpenTelemetry tracing enabled (service.name=backend-auth)
```
