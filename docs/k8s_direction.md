# prod 쿠버네티스 기동 절차

`backend-auth` 를 prod(`dpyb-prod` 클러스터 / `dpyb-auth` 네임스페이스)에 띄우는
순서다. **위에서부터 그대로 따라가면 된다.** 각 단계에 검증 명령이 붙어 있고,
그게 통과해야 다음으로 넘어간다.

관련 문서 — 이 문서는 그 내용을 반복하지 않고 필요한 지점에서 가리킨다.

| 문서 | 다루는 것 |
|---|---|
| `docs/deploy-eks-argocd.md` | dev·prod 공통 배포 구조와 파일 배치 |
| `docs/prod-nodepool-isolation.md` | NodePool 격리 상세 |
| `docs/db-per-service.md` | 서비스별 DB 분리 절차 |
| `docs/production_architecture.md` | 완성된 prod 인프라 구성 |
| `docs/auth_prod_troubleshooting.md` | 실제로 겪은 실패와 원인 |

---

## 0. 전제 — AWS 자격증명부터

**이게 안 되면 아래 전부가 막힌다.** IAM 사용자에게
`kosa-edu-mfa-pol` 이 붙어 있고, 이 정책은 **MFA 세션이 아닌 자격증명의 요청을
전부 explicit deny** 한다. 평문 액세스 키만으로는 `sts:GetCallerIdentity` 외에는
아무것도 호출할 수 없다.

```bash
# 평문 키 프로파일로 MFA 세션을 발급받는다 (12시간)
aws sts get-session-token \
  --serial-number arn:aws:iam::594532711953:mfa/<디바이스명> \
  --token-code <인증앱 6자리> \
  --duration-seconds 43200 \
  --profile dpgy-infra
```

`--serial-number` 의 마지막 부분은 **사용자 이름이 아니라 디바이스 이름**이다.
콘솔 → IAM → 사용자 → 보안 자격 증명 탭에서 확인한다.

반환된 세 값을 `~/.aws/credentials` 에 별도 프로파일로 넣는다. 원본 평문 키
프로파일은 다음 갱신에 필요하므로 **덮어쓰지 않는다.**

```ini
[dpgy-mfa]
aws_access_key_id = <AccessKeyId>
aws_secret_access_key = <SecretAccessKey>
aws_session_token = <SessionToken>
```

```ini
# ~/.aws/config
[profile dpgy-mfa]
region = ap-northeast-2
output = json
```

검증 — `GetCallerIdentity` 는 MFA 없이도 통과하므로 **그것만으로 판단하면 안 된다.**
실제로 deny 대상인 API 를 찔러 봐야 한다.

```bash
export AWS_PROFILE=dpgy-mfa
aws rds describe-db-clusters --region ap-northeast-2 >/dev/null && echo OK
```

---

## 1. kubectl 컨텍스트 확보

```bash
aws eks update-kubeconfig --name dpyb-prod --alias dpyb-prod --region ap-northeast-2
kubectl config use-context dpyb-prod
```

**매 단계 전에 컨텍스트를 확인하는 습관을 들일 것.** dev 와 prod 가 kubeconfig
파일 하나를 공유하므로 다른 터미널에서 dev 를 만지면 이 터미널도 같이 바뀐다.

```bash
kubectl config current-context     # dpyb-prod 여야 한다
```

세션을 아예 분리하고 싶으면:

```bash
export KUBECONFIG=~/.kube/config-prod
aws eks update-kubeconfig --name dpyb-prod --kubeconfig ~/.kube/config-prod
```

---

## 2. 클러스터 1회 설정

**dev 와 prod 는 별도 클러스터라 공유되지 않는다.** prod 컨텍스트에서 각각 적용한다.

```bash
kubectl apply -f k8s/cluster/ingressclass-alb.yaml   # ALB IngressClass
kubectl apply -f k8s/cluster/nodepool-auth.yaml      # auth 전용 NodePool
```

검증:

```bash
kubectl get ingressclass alb
kubectl get nodepool auth
```

NodePool 은 `workload=auth` 라벨과 `dedicated=auth:NoSchedule` taint 를 붙인 노드를
Karpenter 가 필요할 때 프로비저닝하게 한다. 상세는 `docs/prod-nodepool-isolation.md`.

---

## 3. AWS 의존 리소스

### 3.1 Cognito (생성 완료 — 재생성 불필요)

| | 값 |
|---|---|
| User Pool | `ap-northeast-2_v5UmqpECS` (`dpyb-auth-prod`) |
| backend App Client | `3l3m4q175dh6l8u1iuksfp66d2` (`dpyb-auth-backend-prod`) |

App Client 는 secret 이 있고 `ALLOW_USER_PASSWORD_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH`
만 허용한다. 설정 근거는 `docs/auth-architecture.md` §8.1.

Client ID 는 비밀값이 아니므로 ConfigMap(`k8s/overlays/prod/configmap-patch.yaml`)에
이미 들어 있다. **Client Secret 만 k8s Secret 으로 넣는다** (4단계).

새 환경을 처음부터 만드는 경우의 생성 명령:

```bash
aws cognito-idp create-user-pool \
  --pool-name dpyb-auth-<env> \
  --username-attributes email \
  --auto-verified-attributes email \
  --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":true,"TemporaryPasswordValidityDays":7}}' \
  --mfa-configuration OFF \
  --verification-message-template '{"DefaultEmailOption":"CONFIRM_WITH_CODE"}' \
  --deletion-protection ACTIVE

aws cognito-idp create-user-pool-client \
  --user-pool-id <POOL_ID> \
  --client-name dpyb-auth-backend-<env> \
  --generate-secret \
  --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --enable-token-revocation \
  --prevent-user-existence-errors ENABLED \
  --access-token-validity 1 --id-token-validity 1 --refresh-token-validity 30 \
  --token-validity-units '{"AccessToken":"days","IdToken":"days","RefreshToken":"days"}'
```

### 3.2 IRSA (생성 완료 — 재생성 불필요)

보상·복구 경로(`admin_get_user` / `admin_delete_user`)만을 위한 것이다.

| | 값 |
|---|---|
| OIDC provider | `oidc.eks.ap-northeast-2.amazonaws.com/id/ABD868614E3238EEC532C73A888F74BE` |
| IAM Role | `dpyb-auth-irsa-prod` |
| IAM Policy | `dpyb-auth-cognito-admin-prod` (`AdminGetUser`, `AdminDeleteUser` 2개만) |
| trust | `system:serviceaccount:dpyb-auth:backend-auth` |

**클러스터를 새로 만들었다면 OIDC provider 부터 등록해야 한다.** EKS 클러스터의
OIDC issuer 가 있다고 해서 IAM 에 자동 등록되지는 않는다.

```bash
ISSUER=$(aws eks describe-cluster --name dpyb-prod \
  --query 'cluster.identity.oidc.issuer' --output text)
aws iam create-open-id-connect-provider --url "$ISSUER" --client-id-list sts.amazonaws.com
```

검증:

```bash
aws iam list-open-id-connect-providers
aws iam list-attached-role-policies --role-name dpyb-auth-irsa-prod
```

---

## 4. Secret 생성

Secret 은 Git 에 없다. 네임스페이스에 직접 만든다.
구조는 `k8s/secret.example.yaml` 참고.

```bash
kubectl create namespace dpyb-auth --dry-run=client -o yaml | kubectl apply -f -

kubectl -n dpyb-auth create secret generic backend-auth-secret \
  --from-literal=DATABASE_URL='postgresql+psycopg://<user>:<password>@dpyb-prod.cluster-ctk4om8w2c2p.ap-northeast-2.rds.amazonaws.com:5432/dpyb_auth' \
  --from-literal=COGNITO_BACKEND_CLIENT_SECRET='<client secret>'
```

Client Secret 조회:

```bash
aws cognito-idp describe-user-pool-client \
  --user-pool-id ap-northeast-2_v5UmqpECS \
  --client-id 3l3m4q175dh6l8u1iuksfp66d2 \
  --region ap-northeast-2 \
  --query 'UserPoolClient.ClientSecret' --output text
```

### 반드시 지킬 것

**① 키 이름은 `COGNITO_BACKEND_CLIENT_SECRET` 이다.**
`COGNITO_CLIENT_SECRET` 은 Phase 7 에서 폐기된 이름이라 앱이 읽지 않는다.
이름이 틀리면 배포는 성공하고 `/health` 도 200 이지만 `/auth/login` 과
쿠키 `/auth/refresh` 만 500 이 된다.

**② dev Secret 을 복사하지 말 것.** 호스트·DB 이름·비밀번호가 전부 다르다.
셋 중 하나만 남아도 실패한다. 실제로 이 사고가 났고 경위는
`docs/auth_prod_troubleshooting.md` 에 있다.

| | dev | prod |
|---|---|---|
| 호스트 | `dpyb-dev.cluster-...` | `dpyb-prod.cluster-...` |
| DB 이름 | `dpyb_auth` | `dpyb_auth` |
| `admin` 비밀번호 | dev 값 | **prod 값 (다름)** |

**③ 비밀번호에 특수문자가 있으면 percent-encoding 이 필요하다.**
`@ : / # %` 가 들어 있으면 URL 이 깨진다.

```python
from urllib.parse import quote
quote(password, safe="")
```

---

## 5. 데이터베이스 확인

prod Aurora 에는 이미 서비스별 DB 가 만들어져 있다
(`dpyb_auth`, `dpyb_book`, `dpyb_record`). 공용 `dpyb` 는 없다.

없는 환경이라면 생성한다. 빈 DB 로 두면 되고, 스키마는 파드의 initContainer 가
`alembic upgrade head` 로 만든다.

```sql
CREATE DATABASE dpyb_auth;
```

네트워크는 이미 열려 있다 — prod Aurora 와 prod EKS 가 **같은 VPC**
(`vpc-0e6d4633d86f40838`)이고, `dpyb-prod-db-sg` 가 5432 를 prod EKS 클러스터
SG(`sg-0e9160e7485cbc39c`)에서 허용한다. **SG 작업은 필요 없다.**

---

## 6. ArgoCD 등록

ArgoCD 는 **중앙 클러스터에 있고 prod 는 원격 대상**이다. 먼저 클러스터를
등록하지 않으면 `cluster not found` 로 sync 가 실패한다.

```bash
argocd cluster add <prod 컨텍스트> --name dpyb-prod
argocd cluster list                      # dpyb-prod 가 보여야 한다

kubectl apply -f argocd/application-prod.yaml
```

이후는 GitOps 다. `main` 브랜치의 `k8s/overlays/prod` 를 추적하며
`prune: true` + `selfHeal: true` 로 자동 동기화한다.

```bash
argocd app get backend-auth-prod
argocd app sync backend-auth-prod        # 자동 동기화가 안 걸렸을 때만
```

---

## 7. 검증

순서대로 통과해야 한다.

```bash
# 1) 파드가 떴는가
kubectl -n dpyb-auth get pods
#    2/2 Running 이어야 한다. Init:CrashLoopBackOff 면 DB 문제다 (아래 8번)

# 2) 마이그레이션이 돌았는가
kubectl -n dpyb-auth logs -l app.kubernetes.io/name=backend-auth -c migrate --tail=40

# 3) auth 전용 노드에 배치됐는가
kubectl -n dpyb-auth get pods -o wide
kubectl get nodes -l workload=auth

# 4) IRSA 가 주입됐는가
kubectl -n dpyb-auth get sa backend-auth -o jsonpath='{.metadata.annotations}'
kubectl -n dpyb-auth get pod -l app.kubernetes.io/name=backend-auth \
  -o jsonpath='{.items[0].spec.containers[0].env[*].name}'
#    AWS_ROLE_ARN, AWS_WEB_IDENTITY_TOKEN_FILE 가 보여야 한다

# 5) 스모크
ALB=$(kubectl -n dpyb-auth get ingress backend-auth -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
curl -s -o /dev/null -w '%{http_code}\n' "http://$ALB/health"        # 200
curl -s -w '\nHTTP %{http_code}\n' "http://$ALB/api/v1/terms" | tail -3   # 200
```

**`/health` 는 DB 를 타지 않는다.** DB 연결까지 확인하려면 반드시
`GET /api/v1/terms` 를 써야 한다 — 인증이 필요 없고 DB 를 실제로 읽는다.

---

## 8. 막혔을 때 먼저 볼 것

| 증상 | 1순위 확인 |
|---|---|
| `Init:CrashLoopBackOff` | migrate 로그. `ConnectionTimeout` 이면 호스트, `password authentication failed` 면 자격증명 |
| `/auth/login` 만 500 | Secret 의 키 이름이 `COGNITO_BACKEND_CLIENT_SECRET` 인지 |
| 파드가 `Pending` | NodePool 미적용 또는 toleration 누락 |
| ArgoCD `cluster not found` | `argocd cluster add` 미실행 |
| `kubectl logs` 가 `tls: internal error` | 갓 뜬 노드의 kubelet 인증서 미승인. 잠시 후 재시도 |
| AWS CLI 전부 explicit deny | MFA 세션 만료 (0단계) |

전체 경위와 원인 분석은 `docs/auth_prod_troubleshooting.md`.

---

## 아직 남은 것

- **`CORS_ALLOWED_ORIGINS` 가 비어 있다.** prod FE 오리진이 존재하지 않아 넣을 값이
  없다. 비어 있으면 cross-origin 요청이 전혀 허용되지 않으므로, FE 배포 시 반드시
  주입해야 한다. `allow_credentials=true` 라 와일드카드는 쓸 수 없다.
- **prod ALB 가 HTTP:80 이다.** 도메인과 ACM 인증서가 없다. HTTPS 오리진이 생기기
  전까지는 `COOKIE_SECURE=true`(기본값) 상태에서 브라우저가 refresh 쿠키를 저장하지
  못한다. HTTPS 전환은 `k8s/cluster/ingressclass-alb.yaml` 의 `certificateARNs` 와
  Ingress 의 `listen-ports` 에 `{"HTTPS":443}` 추가로 한다.
- **DB 계정이 book 과 공용(`admin`)이다.** dev 와 같은 구성이지만
  database-per-service 취지상 전용 롤 분리가 바람직하다.
