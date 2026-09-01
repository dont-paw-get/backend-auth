# prod 인프라 구성

`backend-auth` 의 **상용 환경 인프라**가 현재 어떻게 구성되어 있는지 정리한다.
애플리케이션 설계(인증 흐름, API 계약, Cognito 오류 매핑)는 범위 밖이며
`docs/auth-architecture.md` 가 소유한다.

기동 절차는 `docs/k8s_direction.md`, 겪은 장애는 `docs/auth_prod_troubleshooting.md`.

---

## 1. 전체 그림

```
                    ┌──────────────────────────────────────────┐
   GitHub           │  AWS 594532711953 / ap-northeast-2       │
   (main)           │                                          │
     │              │   ECR  dpyb-prod/dpyb-auth               │
     │  push        │     ▲                                    │
     ├──────────────┼─────┘ build & push (amd64 + arm64)       │
     │              │                                          │
     │  newTag 커밋 │   ┌────────── VPC vpc-0e6d4633d86f40838 ─┐│
     ▼              │   │                                      ││
   ArgoCD ──sync────┼──▶│  EKS dpyb-prod (Auto Mode, 1.36)     ││
   (중앙 클러스터)   │   │    ns: dpyb-auth                     ││
                    │   │      Deployment backend-auth × 2     ││
   인터넷 ──────────┼──▶│      └ NodePool auth (전용 노드)      ││
        ALB(HTTP:80)│   │            │                         ││
                    │   │            │ 5432                    ││
                    │   │            ▼                         ││
                    │   │  Aurora PostgreSQL dpyb-prod         ││
                    │   │    database: dpyb_auth               ││
                    │   └──────────────────────────────────────┘│
                    │                                          │
                    │   Cognito User Pool  dpyb-auth-prod      │
                    │   IAM Role           dpyb-auth-irsa-prod │
                    └──────────────────────────────────────────┘
```

---

## 2. 기준 정보

| 항목 | 값 |
|---|---|
| AWS 계정 | `594532711953` |
| 리전 | `ap-northeast-2` |
| EKS 클러스터 | `dpyb-prod` (Auto Mode, v1.36) |
| VPC | `vpc-0e6d4633d86f40838` |
| 네임스페이스 | `dpyb-auth` |
| replicas | 2 |
| 노드 OS | Bottlerocket (EKS Auto, Standard) |

dev 는 **완전히 별도 클러스터**(`dpyb-dev`, `vpc-0093ef1d89d6bfd57`)다.
IngressClass·NodePool 같은 클러스터 전역 리소스는 공유되지 않고 각각 적용해야 한다.

---

## 3. 컴퓨트

### 노드 격리

prod 는 `backend-auth` 를 **auth 전용 노드**에만 배치한다.

| 수단 | 값 | 역할 |
|---|---|---|
| NodePool | `auth` (`k8s/cluster/nodepool-auth.yaml`) | Karpenter 가 필요 시 프로비저닝 |
| label | `workload=auth` | Deployment 의 `nodeSelector` 가 이 노드를 고름 |
| taint | `dedicated=auth:NoSchedule` | toleration 없는 다른 서비스 파드 차단 |

label 만으로는 "backend-auth 가 auth 노드로 간다"만 보장되고, 다른 서비스가
끼어드는 것은 taint 로 막는다. **두 개가 한 쌍**이다.

NodePool 제약:

- capacity-type `on-demand`
- instance-category `t`, `m`
- `limits.cpu: 10` — 폭주·비용 방지 상한
- `consolidationPolicy: WhenEmptyOrUnderutilized`, `consolidateAfter: 30s`

적용은 prod 클러스터에만 한다. dev 는 노드를 분리하지 않는다.
상세는 `docs/prod-nodepool-isolation.md`.

### 아키텍처 혼재

클러스터 노드가 **arm64 와 amd64 가 섞여** 있다. Auto Mode 가 스케일아웃할 때
조건에 맞는 인스턴스를 고르기 때문이다. 그래서 이미지를 멀티아키
(`linux/amd64,linux/arm64`)로 빌드한다 — 단일 아키텍처 이미지였을 때
arm64 노드에서 파드가 뜨지 못한 사고가 있었다(커밋 `945a11a`).

### 파드 구성

```
Pod
├── initContainer: migrate      alembic upgrade head (기동마다 1회)
└── container:     backend-auth uvicorn :8000
```

`migrate` 는 앱 컨테이너와 **같은 이미지**를 쓰고 ConfigMap·Secret 을 동일하게
주입받는다. DB 에 붙지 못하면 여기서 실패하므로, DB 문제는 항상 `Init:` 상태로
나타난다.

보안 컨텍스트: `runAsNonRoot`(uid 1000), `readOnlyRootFilesystem`,
`allowPrivilegeEscalation: false`, 모든 capability drop.

probe 는 둘 다 `/health` 를 본다. **`/health` 는 DB 를 타지 않으므로**
DB 가 죽어도 앱 컨테이너는 Ready 로 남는다 — 이 점은 알고 있어야 한다.

---

## 4. 네트워크

### 인그레스

| 항목 | 값 |
|---|---|
| IngressClass | `alb` (controller `eks.amazonaws.com/alb`, Auto Mode 내장) |
| scheme | `internet-facing` |
| listener | **HTTP:80 만** |
| target-type | `ip` |
| health check | `/health` |
| Service | ClusterIP, 80 → `http`(8000) |

**도메인과 TLS 가 없다.** ALB DNS 로 직접 접근한다. HTTPS 로 가려면
`IngressClassParams` 에 `certificateARNs` 를 넣고 Ingress 의 `listen-ports` 에
`{"HTTPS":443}` 을 추가한다.

dev 는 CloudFront(`d1wab52ln5by5k.cloudfront.net`)가 앞에 있어 HTTPS 로 서비스되지만
**prod 에는 CloudFront 가 없다.** 계정의 유일한 CloudFront 배포는 오리진이 전부
dev 리소스다.

### DB 경로

| 항목 | 값 |
|---|---|
| Aurora 클러스터 | `dpyb-prod` (aurora-postgresql 17.7) |
| writer 엔드포인트 | `dpyb-prod.cluster-ctk4om8w2c2p.ap-northeast-2.rds.amazonaws.com:5432` |
| VPC | `vpc-0e6d4633d86f40838` — **EKS 와 동일** |
| subnet group | `default-vpc-0e6d4633d86f40838` |
| 보안 그룹 | `dpyb-prod-db-sg` (`sg-056a2bd9c35613259`) |

`dpyb-prod-db-sg` 의 5432 인바운드:

- `sg-0e9160e7485cbc39c` — prod EKS 클러스터 SG (`eks-cluster-sg-dpyb-prod-...`)
- `121.134.158.113/32` — 운영자 접근용 단일 IP

같은 VPC + SG 참조 방식이라 **파드는 추가 설정 없이 DB 에 도달한다.**

---

## 5. 데이터베이스

서비스별로 DB 를 분리한다. prod Aurora 의 데이터베이스 목록:

```
dpyb_auth, dpyb_book, dpyb_record, postgres, rdsadmin
```

공용 `dpyb` 는 prod 에 없다 — prod 는 처음부터 per-service 로 만들어졌다.
(dev 는 공용 `dpyb` 에서 분리하는 절차를 거쳤고, 그 기록이
`docs/db-per-service.md` 다)

| 항목 | 값 |
|---|---|
| 데이터베이스 | `dpyb_auth` |
| 접속 계정 | `admin` — **book 과 공용** |
| 마스터 계정 | `postgres` |
| 마이그레이션 | Alembic, 버전 테이블 `alembic_version_auth` |

**계정이 서비스 간 공용인 것은 database-per-service 취지와 어긋난다.**
dev 도 같은 구성이라 일관성은 있으나, `dpyb_auth` 전용 롤 분리는 남은 과제다.

접속 정보는 Git 에 없고 `dpyb-auth/backend-auth-secret` 의 `DATABASE_URL`
한 곳에만 존재한다.

---

## 6. 아이덴티티

### Cognito

| | 값 |
|---|---|
| User Pool | `ap-northeast-2_v5UmqpECS` (`dpyb-auth-prod`) |
| backend App Client | `3l3m4q175dh6l8u1iuksfp66d2` (`dpyb-auth-backend-prod`) |

User Pool 설정은 dev 와 동일하게 맞췄다 — email 로그인, email 자동 검증,
비밀번호 최소 8자(대·소문자·숫자·기호), MFA OFF, `CONFIRM_WITH_CODE`,
삭제 보호 ACTIVE, tier ESSENTIALS.

App Client 는 **secret 있음**, 허용 플로우는
`ALLOW_USER_PASSWORD_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH` 두 개뿐이다
(SRP 없음). token revocation 활성, prevent user existence errors 활성,
access·id 1일 / refresh 30일.

dev 와 달리 prod 에는 **FE 용 App Client 가 없다.** backend 전용 하나뿐이다.

### IRSA

파드가 노드 인스턴스 롤이 아니라 자기 전용 IAM 롤을 쓴다.

| 항목 | 값 |
|---|---|
| OIDC provider | `oidc.eks.ap-northeast-2.amazonaws.com/id/ABD868614E3238EEC532C73A888F74BE` |
| ServiceAccount | `backend-auth` (ns `dpyb-auth`) |
| IAM Role | `dpyb-auth-irsa-prod` |
| IAM Policy | `dpyb-auth-cognito-admin-prod` |
| 허용 액션 | `cognito-idp:AdminGetUser`, `cognito-idp:AdminDeleteUser` |
| Resource | prod User Pool ARN 하나로 스코핑 |

**용도는 보상·복구 경로 하나뿐이다.** Cognito SignUp 은 성공했는데 DB INSERT 가
실패했을 때 고아 계정을 정리하기 위한 것이며, 정상 가입·로그인 경로는 이 권한을
쓰지 않는다.

role-arn 어노테이션은 base 가 아니라 overlay
(`k8s/overlays/prod/serviceaccount-patch.yaml`)에서 주입한다. 환경마다 롤이 다르기
때문이다.

> **주의** — role-arn 이 붙는 순간 boto3 는 노드 인스턴스 롤 대신
> `AssumeRoleWithWebIdentity` 를 시도한다. 롤이 없거나 trust policy 의
> 네임스페이스·SA 이름이 틀리면 admin API 뿐 아니라 **SignUp/InitiateAuth 등
> 모든 Cognito 호출이 자격증명 실패로 함께 깨진다.**

### 운영자 접근

IAM 사용자에게 `kosa-edu-mfa-pol` 이 붙어 있고, **MFA 세션이 아닌 요청을
전부 explicit deny** 한다. 평문 액세스 키만으로는 EKS·RDS·Cognito·IAM 어느 것도
조회할 수 없다. `sts:GetSessionToken` 으로 MFA 세션을 발급해 써야 한다.

---

## 7. 설정 주입

| 경로 | 내용 |
|---|---|
| ConfigMap `backend-auth-config` | `APP_ENV`, `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_BACKEND_CLIENT_ID` |
| Secret `backend-auth-secret` | `DATABASE_URL`, `COGNITO_BACKEND_CLIENT_SECRET` |

둘 다 `envFrom` 으로 initContainer 와 앱 컨테이너 **양쪽에** 주입된다.

Client **ID** 는 비밀값이 아니라 ConfigMap 에, Client **Secret** 은 k8s Secret 에
둔다. Secret 은 Git 에 없고 네임스페이스에 직접 만든다
(`k8s/secret.example.yaml` 이 구조만 제공).

### Kustomize 구조

```
k8s/
├── base/            configmap, deployment, service, ingress, serviceaccount
└── overlays/
    ├── dev/         ns dpyb-auth-dev,  replicas 1
    └── prod/        ns dpyb-auth,      replicas 2
                     ├── configmap-patch.yaml       Cognito 실제 값
                     ├── nodepin-patch.yaml         nodeSelector + toleration
                     └── serviceaccount-patch.yaml  IRSA role-arn
```

`images` 트랜스포머가 ECR 경로와 태그를 주입한다. **prod ECR 리포지토리는
IMMUTABLE** 이므로 `main-latest` 같은 movable 태그를 쓰지 않고 항상 커밋 SHA 로만
배포한다.

### 현재 비어 있는 값

| 키 | 상태 |
|---|---|
| `CORS_ALLOWED_ORIGINS` | 미주입 — prod FE 오리진이 아직 없음. 비어 있으면 cross-origin 전면 차단 |
| `COOKIE_SECURE` | 기본값 `true`. ALB 가 HTTP 라 HTTPS 오리진 생기기 전까지 refresh 쿠키가 저장되지 않음 |

---

## 8. 배포 파이프라인

```
main 에 push
  → GitHub Actions (build-push-ecr.yml)
      docker buildx, platforms=linux/amd64,linux/arm64
      → ECR 594532711953.dkr.ecr.ap-northeast-2.amazonaws.com/dpyb-prod/dpyb-auth
      → k8s/overlays/prod/kustomization.yaml 의 newTag 를 커밋 SHA 로 갱신 [skip ci]
  → ArgoCD 가 그 커밋 감지 → 동기화
```

ArgoCD Application `backend-auth-prod`:

| 항목 | 값 |
|---|---|
| repo | `github.com/dont-paw-get/backend-auth.git` |
| branch | `main` |
| path | `k8s/overlays/prod` |
| destination | `name: dpyb-prod` (원격 클러스터), ns `dpyb-auth` |
| syncPolicy | `automated` + `prune` + `selfHeal`, `CreateNamespace=true` |

**ArgoCD 는 prod 클러스터 안이 아니라 중앙 클러스터에 있다.** 따라서
`argocd cluster add ... --name dpyb-prod` 로 원격 클러스터를 먼저 등록해야 하고,
미등록 상태면 `cluster not found` 로 sync 가 실패한다.

CI 는 GitHub Secrets 의 AWS 액세스 키로 인증한다(OIDC 아님).
경위는 `docs/adr` 가 아니라 dpgy-infra 저장소의 ADR-0006 에 있다.

---

## 9. 관측

이 저장소에는 관측 스택이 없다. 메트릭·로그·트레이스 백엔드와 알림은
`dpgy-infra` 저장소가 소유하며, 서비스 저장소의 책임은 텔레메트리를
**올바른 형식으로 내보내는 것**까지다.

| 항목 | 상태 |
|---|---|
| stdout JSON 구조화 로그 (-> Alloy -> Loki) | **적용됨** |
| OpenTelemetry 분산 추적 (-> OTLP -> Collector -> Tempo) | **dev 적용됨** (`otel-collector.monitoring:4318`), prod 는 collector 주소 주입 대기 |
| `/actuator/prometheus` 대응 메트릭 + `ServiceMonitor` CR | 미적용 |

로깅·추적의 구현과 운영 방법은 `docs/observability.md` 에 있다. 요약하면,

- 로그는 `app/core/logging_config.py` 가 stdout 에 한 줄 JSON 으로 내보낸다.
  파드 stdout 을 Alloy 가 수집하므로 앱 쪽 추가 설정은 없다.
- 추적은 `app/core/tracing.py` 가 담당하며, `OTEL_EXPORTER_OTLP_ENDPOINT`
  가 주입된 환경에서만 켜진다. dev overlay 에는 이 값이 주입돼 있어
  (`http://otel-collector.monitoring.svc.cluster.local:4318`, `OTEL_TRACES_SAMPLER`
  `parentbased_traceidratio`/`1.0` = 전량) trace 가 Tempo 로 나간다.
  prod overlay 는 아직 주소 미확인이라 로그만 나간다.
- 메트릭은 여전히 미적용이다.

---

## 10. 알려진 제약

| 항목 | 내용 |
|---|---|
| TLS 없음 | ALB HTTP:80. 도메인·ACM 인증서 미확보 |
| CORS 미설정 | prod FE 오리진 부재로 값을 넣을 수 없음 |
| 쿠키 저장 불가 | HTTPS 오리진이 없어 `COOKIE_SECURE=true` 와 충돌 |
| DB 계정 공용 | `admin` 을 book 과 공유 |
| rate limit 인메모리 | replicas 2 이므로 실효 한도가 2배가 된다 |
| 메트릭 미적용 | 구조화 로그·트레이스는 적용됐으나 Prometheus 메트릭은 없음 |
| 트레이스 미수집 | 코드는 준비됐으나 `OTEL_EXPORTER_OTLP_ENDPOINT` 미주입 |
| CI 정적 키 | GitHub OIDC 가 아니라 액세스 키 사용 |
