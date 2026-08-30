# 서비스별 데이터베이스 분리 (dev)

`backend-auth` 가 사용하는 데이터베이스를 세 서비스 공용 `dpyb` 에서 auth 전용
`dpyb_auth` 로 분리한 작업 기록입니다. **dev 전환 완료: 2026-08-30.**

- Aurora 클러스터: `dpyb-dev` (PostgreSQL 17.7). 클러스터도 인스턴스도 늘리지 않고
  **데이터베이스만** 나눴으므로 추가 비용은 없습니다.
- 데이터베이스 이름: **dev·prod 모두 `dpyb_auth`**
- 다른 서비스: `dpyb_record`, `dpyb_book`

## 왜 나눴나

MSA 원칙상 각 서비스가 자기 데이터베이스를 소유해야 하는데, 실제로는 `dpyb`
데이터베이스의 `public` 스키마 하나에 세 서비스 테이블 13개가 스키마 분리도 없이
들어 있었습니다. PostgreSQL 은 데이터베이스 간 JOIN 이 `dblink` 없이는 불가능하므로,
데이터베이스를 나누면 서비스 경계가 규율이 아니라 엔진 차원에서 강제됩니다.

## 레포에는 무엇이 바뀌었나 — 사실상 없음

데이터베이스 이름은 `backend-auth-secret` 의 `DATABASE_URL` 에만 존재하고, 그 Secret 은
Git 에도 ArgoCD 관리 대상에도 없습니다(`k8s/secret.example.yaml` 참고). 매니페스트는
`envFrom.secretRef` 로 참조만 하므로 코드·매니페스트 수정 없이 Secret patch 와 파드
재생성만으로 전환이 끝납니다. 이 문서와 각 파일의 주석이 바뀐 전부입니다.

## 전환 절차

prod 적용이나 재현이 필요할 때 쓰는 절차입니다. `NS`, `SEC`, `NEW_DB` 만 환경에 맞게
바꾸면 됩니다 (prod: `NS=dpyb-auth`, `NEW_DB=dpyb_auth`).

```bash
NS=dpyb-auth-dev
SEC=backend-auth-secret
APP=backend-auth
NEW_DB=dpyb_auth

# 0) 대상 클러스터 확인 — prod 컨텍스트에서 실수로 돌리지 않도록
kubectl config current-context

# 1) 현재 값 백업 (자격증명 포함 — 커밋 금지)
kubectl -n $NS get secret $SEC -o jsonpath='{.data.DATABASE_URL}' \
  | base64 -d > ~/dburl-$NS.bak

# 2) 마지막 경로 조각만 치환 (호스트·계정·비밀번호·쿼리 파라미터 보존)
CUR=$(cat ~/dburl-$NS.bak)
NEXT=$(echo "$CUR" | sed -E "s#/[^/?]*(\?[^#]*)?\$#/$NEW_DB\1#")
echo "$NEXT" | sed -E 's#://[^:]+:[^@]+@#://***:***@#'   # 눈으로 검증

kubectl -n $NS patch secret $SEC --type merge \
  -p "{\"stringData\":{\"DATABASE_URL\":\"$NEXT\"}}"

# 3) 파드 재생성 (환경변수는 파드 생성 시점에 주입되므로 필수)
kubectl -n $NS delete pod -l app.kubernetes.io/name=$APP
```

`rollout restart` 대신 `delete pod` 를 쓰는 이유: `rollout restart` 는 Deployment 에
어노테이션을 추가하는데, ArgoCD 가 `selfHeal` 로 이를 Git 과의 차이로 보고 되돌리면서
롤아웃이 한 번 더 돕니다.

### PowerShell 에서의 주의점

`kubectl patch -p $json` 은 PowerShell 이 인자에서 큰따옴표를 벗겨내 `invalid character
's' looking for beginning of object key string` 로 실패합니다. `--patch-file` 을 쓰고,
파일은 **BOM 없이** 써야 합니다 (`Out-File -Encoding utf8` 은 BOM 을 붙입니다).

```powershell
$patch = @{ stringData = @{ DATABASE_URL = $next } } | ConvertTo-Json -Compress
$tmp = Join-Path $env:TEMP 'auth-db-patch.json'
[IO.File]::WriteAllText($tmp, $patch, (New-Object Text.UTF8Encoding $false))
kubectl -n $NS patch secret $SEC --type merge --patch-file $tmp
Remove-Item $tmp -Force
```

## 전환 시 확인할 auth 고유 항목

행 수만 맞춰 보고 넘어가면 안 되는 항목들입니다. 새 DB 를 상대로 확인하세요.

```sql
-- 1) Alembic 리비전이 head 인가
--    deployment 의 initContainer 가 파드 기동마다 `alembic upgrade head` 를 돌린다.
--    head 가 아니면 새 DB 에 마이그레이션이 실제로 실행된다.
select version_num from alembic_version_auth;

-- 2) partial unique index 가 predicate 까지 복사됐는가 (CLIAR-177 탈퇴 후 재가입)
--    빠지거나 predicate 가 사라지면 재가입이 조용히 깨진다.
select indexdef from pg_indexes where indexname = 'uq_member_email_active';
select conname from pg_constraint where conname = 'uq_users_email';  -- 0행이어야 정상

-- 3) member_status enum 라벨 3개
--    PENDING 은 9b41c7d2e5f3 에서 ALTER TYPE ADD VALUE 로 나중에 추가됐다.
select enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid
 where t.typname = 'member_status';   -- ACTIVE, WITHDRAWN, PENDING

-- 4) identity 시퀀스 위치
--    is_called=false 이거나 last_value 가 뒤처져 있으면 다음 INSERT 가 PK 중복으로 실패한다.
select last_value, is_called from member_id_seq;
```

앱이 실제로 새 DB 에 붙었는지는 자격증명을 읽지 않고도 확인할 수 있습니다.

```sql
select datname, count(*) from pg_stat_activity
 where datname in ('dpyb', 'dpyb_auth') group by datname;
```

`GET /api/v1/terms` 는 인증이 필요 없고 DB 를 읽으므로 스모크 테스트로 적합합니다
(`/health` 는 DB 를 타지 않습니다).

## 롤백

테이블은 옮긴 게 아니라 **복사**했으므로 원본 `dpyb` 가 그대로 있습니다.

```bash
kubectl -n $NS patch secret $SEC --type merge \
  -p "{\"stringData\":{\"DATABASE_URL\":\"$(cat ~/dburl-$NS.bak)\"}}"
kubectl -n $NS delete pod -l app.kubernetes.io/name=$APP
```

## 남은 일

- **원본 `dpyb` 데이터베이스를 삭제하지 않았습니다.** 세 서비스가 모두 새 DB 에서
  안정적으로 도는 것을 확인한 뒤 다 같이 정리합니다.
- **prod 는 아직 적용하지 않았습니다.** prod 는 처음부터 `dpyb_auth` 이름으로 만듭니다.
- 백업 파일 `~/dburl-*.bak` 은 전환이 안정된 뒤 삭제하세요.
