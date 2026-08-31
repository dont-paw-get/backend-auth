# prod 구성 트러블슈팅 기록

`backend-auth` 를 prod 에 올리면서 실제로 막혔던 지점들이다. 대부분
**"배포는 성공했는데 동작하지 않는"** 유형이라 기록해 둘 가치가 있다.

각 항목은 증상 → 원인 → 해결 순으로 쓰고, 다음에 같은 걸 만났을 때 어디를 먼저
볼지를 함께 적는다.

기동 절차는 `docs/k8s_direction.md`, 완성된 구성은 `docs/production_architecture.md`.

---

## 1. AWS CLI 가 전부 explicit deny

**증상**

서로 무관한 API 가 전부 같은 정책에서 거부된다.

```
AccessDeniedException: User: arn:aws:iam::594532711953:user/kosa12 is not authorized
to perform: eks:ListClusters ... with an explicit deny in an identity-based policy:
arn:aws:iam::594532711953:policy/kosa-edu-mfa-pol
```

`eks:ListClusters`, `rds:DescribeDBClusters`, `cognito-idp:ListUserPools`,
`iam:ListOpenIDConnectProviders` — 넷 다 동일.

**원인**

권한이 빠진 게 아니라 **MFA 조건부 deny** 다. `kosa-edu-mfa-pol` 이
`aws:MultiFactorAuthPresent: false` 인 요청을 전부 막는다. 평문 액세스 키에는
MFA 세션이 없으므로 그대로 걸린다.

`sts:GetCallerIdentity` 는 보통 deny 대상에서 빠지기 때문에 **"자격증명은
유효한데 아무것도 안 되는"** 혼란스러운 상태가 된다.

**해결**

```bash
aws sts get-session-token \
  --serial-number arn:aws:iam::594532711953:mfa/<디바이스명> \
  --token-code <6자리> --duration-seconds 43200 --profile dpgy-infra
```

반환된 3개 값(`AccessKeyId`/`SecretAccessKey`/**`SessionToken`**)을 별도 프로파일로
넣는다. 평문 키 프로파일은 다음 갱신에 필요하므로 덮어쓰지 않는다.

**놓치기 쉬운 것**

- `--serial-number` 의 마지막 부분은 **사용자 이름이 아니라 디바이스 이름**이다.
  `mfa/kosa12` 로 가정했다가 `MFA serial number is valid and associated with this
  user` 오류를 봤다. 콘솔 → IAM → 사용자 → 보안 자격 증명 탭에서 확인한다.
- TOTP 는 30초마다 바뀌고 재사용이 안 된다. 붙여넣는 사이에 만료되면 같은 오류가 난다.
- 등록된 것이 passkey/FIDO 뿐이면 `--token-code` 방식 자체가 불가능하다.
- 이전에 쓰던 프로파일에 만료된 `aws_session_token` 이 남아 있으면, 새 평문 키가
  유효해도 `InvalidClientTokenId` 가 계속 난다. 장기 키로 쓸 프로파일에서는
  그 줄을 지워야 한다.

**먼저 볼 것** — 검증은 `GetCallerIdentity` 가 아니라 실제 deny 대상 API 로 한다.

```bash
aws rds describe-db-clusters --region ap-northeast-2 >/dev/null && echo OK
```

---

## 2. migrate initContainer 가 ConnectionTimeout 으로 무한 재시작

**증상**

```
NAME                            READY   STATUS                  RESTARTS
backend-auth-7cf6789c67-8sd26   0/1     Init:CrashLoopBackOff   234
```

```
sqlalchemy.exc.OperationalError: (psycopg.errors.ConnectionTimeout)
connection timeout expired
```

4일 넘게 `0/2` 상태였다.

**원인**

`backend-auth-secret` 의 `DATABASE_URL` 이 **dev Aurora** 를 가리키고 있었다.
dev Secret 을 복사해 만든 것이다.

```
dev Aurora    vpc-0093ef1d89d6bfd57
prod EKS      vpc-0e6d4633d86f40838   ← VPC 가 다르다
```

VPC 가 다르니 라우팅 자체가 없다. 그래서 연결 거부가 아니라 **타임아웃**이다.

**해결**

호스트를 `dpyb-prod.cluster-ctk4om8w2c2p.ap-northeast-2.rds.amazonaws.com` 로 교체.

**교훈**

네트워크를 의심하기 전에 **양쪽 VPC 를 먼저 비교**하면 빠르다. 확인해 보니
prod Aurora 는 prod EKS 와 같은 VPC 에 있고, `dpyb-prod-db-sg` 가 5432 를
prod EKS 클러스터 SG(`sg-0e9160e7485cbc39c`)에서 이미 허용하고 있었다.
**SG 작업은 애초에 필요 없었다.**

```bash
aws eks describe-cluster --name dpyb-prod --query 'cluster.resourcesVpcConfig.vpcId'
aws rds describe-db-clusters --db-cluster-identifier dpyb-prod \
  --query 'DBClusters[0].DBSubnetGroup'
aws ec2 describe-security-groups --group-ids <db-sg> --query 'SecurityGroups[0].IpPermissions'
```

---

## 3. 호스트를 고쳤더니 이번엔 인증 실패

**증상**

```
FATAL: password authentication failed for user "admin"
```

**원인**

같은 뿌리다 — dev Secret 복사본이라 **비밀번호도 dev 값**이었다.
`admin` 계정 자체는 prod Aurora 에도 있었고 비밀번호만 달랐다.

**해결**

prod 에서 정상 동작 중인 `dpyb-book/backend-book-secret` 을 출처로 삼았다.

**진단 방법**

Secret 값을 직접 읽지 않고도 판별할 수 있었다. `kubectl describe secret` 은
**키 이름과 바이트 수만** 보여준다.

```
DB_USERNAME:  5 bytes      ← "admin" 과 정확히 일치
DB_PASSWORD:  32 bytes
```

book 은 prod 에서 멀쩡히 도는데 사용자명이 5바이트 → `admin` 은 prod 에 존재하고
비밀번호만 다르다는 결론이 나온다.

**교훈**

`ConnectionTimeout` → `password authentication failed` 로 **오류가 바뀐 것 자체가
진전의 증거**다. Postgres 까지 도달했다는 뜻이다. 오류 메시지의 변화를 단계별
체크포인트로 쓰면 원인을 하나씩 벗겨낼 수 있다.

---

## 4. DB 이름이 구 공용 DB 를 가리키고 있었다

**증상**

인증까지 통과했는데도 목표 상태가 아니었다.

```
TARGET: dpyb-prod.cluster-...:5432/dpyb
```

**원인**

`dpyb` 는 세 서비스가 공유하던 구 DB 다. `docs/db-per-service.md` 기준
auth 는 **`dpyb_auth`** 를 써야 한다.

즉 dev Secret 복사로 인한 문제가 **호스트·비밀번호·DB 이름 세 겹**이었다.

**해결**

`/dpyb` → `/dpyb_auth`.

**뜻밖의 사실**

prod Aurora 의 DB 목록을 확인해 보니 이미 per-service 로 되어 있었다.

```
dpyb_auth, dpyb_book, dpyb_record, postgres, rdsadmin
```

공용 `dpyb` 가 아예 없다. `docs/db-per-service.md` 의 "prod 는 아직 적용하지
않았습니다" 는 낡은 서술이다. **문서보다 실물을 먼저 확인해야 했다.**

---

## 5. URL 조립 시 비밀번호 인코딩

**함정**

`DATABASE_URL` 을 문자열로 이어 붙이면 비밀번호의 `@ : / # %` 가 URL 구분자로
해석되어 깨진다. 32자 랜덤 비밀번호에는 충분히 들어갈 수 있다.

**해결**

percent-encoding 을 적용한다.

```python
from urllib.parse import quote
quote(password, safe="")   # @ : / 까지 전부 인코딩
```

---

## 6. `COGNITO_BACKEND_CLIENT_SECRET` 키가 아예 없었다

**증상**

아직 발현 전에 발견했다. 그대로 뒀다면 `/health` 는 200 인데
`/auth/login` 과 쿠키 `/auth/refresh` 만 500 이 되는 형태로 나타났을 것이다.

**원인**

`kubectl describe secret backend-auth-secret`:

```
COGNITO_CLIENT_SECRET:  0 bytes      ← Phase 7 에서 폐기된 이름, 앱이 안 읽음
DATABASE_URL:           109 bytes
```

앱이 실제로 읽는 이름은 `COGNITO_BACKEND_CLIENT_SECRET` 인데 그 키가 없었다.
`app/core/config.py` 에 `COGNITO_CLIENT_SECRET` 은 정의조차 되어 있지 않다.

없으면 `secret_hash()` 가 `RuntimeError` 를 던진다 — 로그인과 갱신 경로에서만
터지므로 헬스체크로는 잡히지 않는다.

**해결**

올바른 이름으로 주입.

```bash
aws cognito-idp describe-user-pool-client \
  --user-pool-id ap-northeast-2_v5UmqpECS \
  --client-id 3l3m4q175dh6l8u1iuksfp66d2 \
  --query 'UserPoolClient.ClientSecret' --output text
```

폐기된 `COGNITO_CLIENT_SECRET`(0바이트)은 앱이 읽지 않아 무해하므로 남겨 뒀다.

**먼저 볼 것** — 설정 키 이름은 매니페스트가 아니라 `app/core/config.py` 의
필드 정의를 기준으로 확인한다. 주석에 언급된 이름과 실제 필드는 다를 수 있다.

---

## 7. prod Cognito User Pool 이 존재하지 않았다

**증상**

`docs/auth-architecture.md` §2.4 에 prod 값이 placeholder 로 적혀 있었는데,
"아직 안 적었을 뿐 실물은 있겠지" 가 아니라 **정말로 없었다.**

```bash
aws cognito-idp list-user-pools --max-results 30
# → User Pool 이 계정 전체에 dev 하나뿐
```

**해결**

dev 설정을 API 로 읽어 같은 값으로 prod 를 생성했다. 사양서만 보고 만들지 않고
실제 dev 를 복사한 이유는, 문서에 안 적힌 설정(비밀번호 정책 세부, 계정 복구
메커니즘, tier 등)까지 맞추기 위해서다.

```bash
aws cognito-idp describe-user-pool --user-pool-id ap-northeast-2_y1mKz50El
```

**순서 의존성** — IRSA 의 IAM Policy 는 Resource 를 User Pool ARN 으로 좁혀야
하므로 **User Pool 생성이 먼저**여야 한다.

---

## 8. prod EKS 의 OIDC provider 가 IAM 에 등록돼 있지 않았다

**증상**

IRSA 를 붙이려는데 dev 는 되고 prod 는 안 될 상황이었다.

```bash
aws iam list-open-id-connect-providers
# → dev EKS(846614EE...)와 GitHub Actions 용 둘뿐. prod(ABD86861...) 없음
```

**원인**

EKS 클러스터에 OIDC issuer 가 있다고 해서 IAM 에 자동 등록되지는 않는다.
**별도 작업**이다.

**해결**

```bash
ISSUER=$(aws eks describe-cluster --name dpyb-prod \
  --query 'cluster.identity.oidc.issuer' --output text)
aws iam create-open-id-connect-provider --url "$ISSUER" --client-id-list sts.amazonaws.com
```

---

## 9. role-arn 을 미리 넣으면 안 되는 이유

**함정**

IAM 롤을 만들기 전에 매니페스트에 role-arn 어노테이션을 넣어 두고 싶어지지만,
**절대 하면 안 된다.**

어노테이션이 붙는 순간 EKS 는 파드에 `AWS_ROLE_ARN` /
`AWS_WEB_IDENTITY_TOKEN_FILE` 을 주입하고, boto3 는 노드 인스턴스 롤 대신
`AssumeRoleWithWebIdentity` 를 시도한다. 롤이 없으면 그 호출이 실패하고
**`AdminGetUser` 같은 admin API 뿐 아니라 `SignUp`/`InitiateAuth` 등 모든 Cognito
호출이 자격증명 없음으로 함께 깨진다.**

trust policy 의 네임스페이스나 SA 이름이 틀려도 결과는 같다.

**대응**

패치 파일은 미리 작성하되 `kustomization.yaml` 에서는 주석 상태로 두고, 롤이
실제로 만들어진 뒤에 활성화했다.

어노테이션 **없는** ServiceAccount 는 default SA 와 자격증명 동작이 동일하므로
(노드 롤 사용) 그 상태로 배포하는 것은 무해하다.

**검증**

```bash
kubectl -n dpyb-auth get sa backend-auth -o jsonpath='{.metadata.annotations}'
kubectl -n dpyb-auth get pod -l app.kubernetes.io/name=backend-auth \
  -o jsonpath='{.items[0].spec.containers[0].env[*].name}'
```

dev 에서 먼저 확인한 뒤 prod 로 넘기는 것이 안전하다.

---

## 10. kubectl 컨텍스트가 조용히 dev 로 바뀌어 있었다 ⚠️

**증상**

prod Secret 을 수정하는 스크립트가 실패했다.

```
Error from server (NotFound): namespaces "dpyb-auth" not found
```

**원인**

현재 컨텍스트가 `dpyb-dev` 였다. kubeconfig 는 파일 하나를 공유하므로 다른
터미널에서 dev 로 전환하면 이 터미널도 같이 바뀐다.

**아슬아슬했던 지점**

실패한 이유가 "dev 클러스터에는 `dpyb-auth` 네임스페이스가 없어서" 였다.
dev 는 `dpyb-auth-dev` 를 쓴다. **이름이 우연히 달라서 막힌 것이지 안전장치가
아니었다.** 이름이 같았다면 정상 동작 중인 dev Secret 을 덮어썼을 것이다.

게다가 dev 클러스터에도 `dpyb-book` 네임스페이스가 존재해서, 자격증명을 book 에서
읽어오는 이 스크립트는 **dev 값을 읽어 prod 에 쓰는** 조합까지 가능했다.

**해결**

prod 를 건드리는 스크립트 맨 앞에 대상 클러스터 검증을 넣었다.

```python
context = run(["kubectl", "config", "current-context"])
if "dpyb-prod" not in context:
    sys.exit(f"현재 컨텍스트가 prod 가 아닙니다: {context}")
```

세션 단위로 격리하려면:

```bash
export KUBECONFIG=~/.kube/config-prod
aws eks update-kubeconfig --name dpyb-prod --kubeconfig ~/.kube/config-prod
```

**교훈** — 파괴적 작업에는 "대상이 맞는지" 확인을 코드에 넣는다. 이름이 달라서
막히는 건 운이다.

---

## 11. `kubectl logs` 가 `tls: internal error`

**증상**

진단 파드는 정상 실행됐는데 로그만 못 읽는다.

```
Error from server: Get "https://10.0.151.229:10250/containerLogs/...":
remote error: tls: internal error
```

**원인**

그 파드가 **방금 생성된 노드**(생성 50초)에 배치됐고, kubelet serving 인증서가
아직 승인되기 전이라 API 서버가 로그를 프록시하지 못했다. EKS Auto Mode 가
스케일아웃하면서 새 노드를 띄운 것이다.

같은 시점에 기존 노드의 파드는 로그가 정상적으로 읽혔다.

**해결**

진단 파드를 이미 떠 있는 auth 노드에 고정했다.

```yaml
nodeSelector:
  workload: auth
tolerations:
  - key: dedicated
    operator: Equal
    value: auth
    effect: NoSchedule
```

새 노드를 띄우지 않아 실행도 빨라진다.

**부수적 발견** — 새로 뜬 노드만 amd64 였고 기존 4개는 arm64 였다. 멀티아키
이미지라 동작에는 문제가 없지만, 노드 아키텍처가 섞이는 구성이라는 점은 알고
있어야 한다.

---

## 12. 마이그레이션이 no-op 이었다

**증상**

파드는 2/2 Running 이 됐는데 migrate 로그가 두 줄뿐이었다.

```
INFO [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO [alembic.runtime.migration] Will assume transactional DDL.
```

`Running upgrade` 가 없다 = **이미 head 였다.**

**의미**

빈 DB 에 전체 마이그레이션이 도는 것을 기대했는데 그렇지 않았다. 즉 이 DB 의
스키마를 이번 경로로 만들지 않았다는 뜻이고, **리비전이 head 라는 사실만으로는
구조가 실제로 맞는지 알 수 없다.**

**확인 결과 — 전부 정상**

| 확인 | 결과 |
|---|---|
| alembic 리비전 | `205eb1a0a7eb` — 저장소 체인 대조 결과 head 맞음 |
| `uq_member_email_active` | `(email) WHERE (deleted_at IS NULL)` — **predicate 유지** |
| `uq_users_email` | 0행 (정상) |
| `member_status` enum | ACTIVE / PENDING / WITHDRAWN |
| `member_id_seq` | `last_value=1, is_called=false` |
| `member` 행 수 | 0 |

`dpyb_auth` 는 alembic 이 정상적으로 전체 마이그레이션을 수행한 DB 였다.
이번 기동에서 no-op 이었던 것은 이미 완료돼 있었기 때문이다.
`GET /api/v1/terms` 가 약관 3건을 반환하는 것도 seed 마이그레이션
(`aa3c28032296`)이 돌았다는 방증이다.

> `is_called=false` 는 db-per-service.md 가 경고한 상황이 **아니다.** 그 경고는
> 행을 복사해 넣고 시퀀스를 올리지 않은 경우를 말한다. 여기는 member 행이 0개라
> 충돌 대상이 없고, 한 번도 쓰지 않은 시퀀스의 정상 상태다. 다음 INSERT 가 1을 받는다.

**확인 방법**

`docs/db-per-service.md` 의 auth 고유 항목 4종을 새 DB 상대로 직접 본다.

```sql
select version_num from alembic_version_auth;

-- predicate 까지 살아 있어야 한다. 빠지면 탈퇴 후 재가입이 조용히 깨진다 (CLIAR-177)
select indexdef from pg_indexes where indexname = 'uq_member_email_active';
select conname from pg_constraint where conname = 'uq_users_email';   -- 0행이어야 정상

-- PENDING 은 나중에 ALTER TYPE ADD VALUE 로 추가된 값이다
select enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid
 where t.typname = 'member_status';   -- ACTIVE, WITHDRAWN, PENDING

select last_value, is_called from member_id_seq;
```

---

## 13. 진단 기법 — 자격증명을 노출하지 않고 조사하기

Secret 값을 직접 읽지 않고 조사해야 하는 상황이 반복됐다. 두 가지가 유용했다.

### `describe secret` — 키 이름과 크기만

```bash
kubectl -n dpyb-book describe secret backend-book-secret
```

값은 안 나오고 `DB_USERNAME: 5 bytes` 처럼 크기만 나온다. 이것만으로
`admin` 을 특정할 수 있었다(3번).

### `envFrom` 진단 파드 — 자격증명이 밖으로 안 나감

Secret 을 파드 안으로만 주입하고, 출력에서는 `@` 앞부분을 잘라낸다.

```yaml
spec:
  containers:
    - name: dbcheck
      image: <앱과 동일한 이미지>
      command: ["python", "-c"]
      args:
        - |
          import os, re, psycopg
          dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
          print("TARGET:", re.sub(r"^.*@", "", dsn))          # user:password 제거
          admin = dsn.rsplit("/", 1)[0] + "/postgres"          # 목표 DB 가 없을 수 있으므로
          with psycopg.connect(admin, connect_timeout=10) as conn:
              rows = conn.execute(
                  "select datname from pg_database where datistemplate = false order by 1"
              ).fetchall()
              print("DATABASES:", ", ".join(r[0] for r in rows))
      envFrom:
        - secretRef:
            name: backend-auth-secret
```

앱과 같은 이미지를 쓰므로 psycopg 가 이미 들어 있고, **앱이 실제로 겪는 것과
동일한 네트워크·자격증명 조건**에서 확인된다는 점이 중요하다.

### 파드를 새로 못 띄울 때 — 떠 있는 파드에 exec

파드 생성 권한이 없거나 새 노드 이슈(11번)를 피하고 싶으면, 이미 실행 중인
파드에 그대로 붙는 편이 빠르다. 환경변수와 라이브러리가 이미 다 있다.

```bash
POD=$(kubectl -n dpyb-auth get pods -l app.kubernetes.io/name=backend-auth         -o jsonpath='{.items[0].metadata.name}')
kubectl -n dpyb-auth exec "$POD" -c backend-auth -- python -c "
import os, psycopg
dsn = os.environ['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://')
with psycopg.connect(dsn, connect_timeout=10) as conn:
    print(conn.execute('select version_num from alembic_version_auth').fetchall())
"
```

리소스를 만들지 않으므로 뒷정리도 필요 없다. 실제 스키마 검증(12번)은 이 방식으로 했다.

접속 대상을 `postgres` 로 바꿔 여는 이유는, 목표 DB 가 아직 없을 수 있어
거기 직접 붙으면 존재 여부를 확인할 수 없기 때문이다.

---

## 요약 — 다음에 막히면 이 순서로

| 순서 | 확인 | 명령 |
|---|---|---|
| 1 | MFA 세션이 살아 있는가 | `aws rds describe-db-clusters` (GetCallerIdentity 아님) |
| 2 | kubectl 컨텍스트가 prod 인가 | `kubectl config current-context` |
| 3 | 파드가 `Init:` 에서 죽는가 | `kubectl logs <pod> -c migrate` |
| 4 | 타임아웃인가 인증 실패인가 | 오류 문구로 원인 계층 판별 |
| 5 | Secret 의 키 이름이 맞는가 | `kubectl describe secret` + `app/core/config.py` |
| 6 | DB 에 실제로 붙는가 | `GET /api/v1/terms` (`/health` 는 DB 를 안 탄다) |
