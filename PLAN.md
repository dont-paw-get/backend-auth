# 인증 전환 작업 계획 (TODO)

BE 주도 Cognito 인증 전환(CLIAR-148 계열)의 **남은 작업만** 추적한다.

설계 결정·흐름·API 계약·Cognito 오류 매핑·보안 정책·구현 현황 등 명세는
`docs/auth-architecture.md`로 분리했다. 이 문서에는 아직 하지 않은 일만 남긴다.

## 진행 현황

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 신규 App Client 생성 / IRSA Role·Policy | 완료 (CLIAR-183) — dev/prod App Client·IRSA Role 생성 |
| 1 | 설정·공통 기반 (`config` / `cognito_auth` / `cognito_errors` / `cookies`) | 완료 |
| 2 | DB 마이그레이션(`PENDING`) + 모델·deps·repository | 완료 |
| 3 | 회원가입 3종 + 보상·복구 | 완료 (보상 경로는 IRSA 의존, B 참고) |
| 4 | 로그인/갱신/로그아웃 + 쿠키 + CORS | 완료 |
| 5 | 비밀번호 3종 | 완료 |
| 6 | Rate limiting + 보안 하드닝 + 감사 로그 | 완료 |
| 7 | `/users/bootstrap` 제거, 매니페스트 갱신, dev 통합 검증 | 완료 (CLIAR-162 / CLIAR-175) |
| 8 | FE 연동 가이드 문서화 | **미완료** (C 참고) |

Phase 4에서 넘겼던 미해결 이슈(`verify_cognito_token`이 검사하는 `client_id`가
기존 FE App Client와 충돌하던 문제)는 CLIAR-162 Phase 7에서 해소됐다 —
`_required_client_id()`가 `COGNITO_BACKEND_CLIENT_ID`를 사용한다.

## A. prod 데이터베이스 연결 — 해결 (CLIAR-183)

prod `backend-auth`가 **2/2 Running** 이고 `/health` 200,
`GET /api/v1/terms` 가 `dpyb_auth` 에서 약관 3건을 정상 반환한다.

### 원인 — 세 가지가 겹쳐 있었다

`backend-auth-secret` 의 `DATABASE_URL` 이 dev Secret 을 복사한 것이라,
호스트뿐 아니라 DB 이름과 비밀번호까지 전부 dev 값이었다.

| | 이전 | 이후 |
|---|---|---|
| 호스트 | `dpyb-dev.cluster-...` (다른 VPC → ConnectionTimeout) | `dpyb-prod.cluster-ctk4om8w2c2p...` |
| DB | `dpyb` (구 공용 DB) | `dpyb_auth` |
| 자격증명 | dev `admin` 비밀번호 (→ password authentication failed) | prod `admin` 비밀번호 |

호스트만 고쳤을 때 타임아웃이 인증 오류로 바뀌면서 두 번째·세 번째 원인이 드러났다.
`admin` 계정 자체는 prod 에도 있었고 **비밀번호만 달랐다** — book 이 prod 에서
정상 동작 중이라 `dpyb-book/backend-book-secret` 을 출처로 삼았다.

### 확인된 환경 사실

- prod Aurora `dpyb-prod.cluster-ctk4om8w2c2p.ap-northeast-2.rds.amazonaws.com:5432`
- prod Aurora VPC = prod EKS VPC (`vpc-0e6d4633d86f40838`) — **동일**
- `dpyb-prod-db-sg` 가 5432 를 prod EKS 클러스터 SG 에서 이미 허용 → SG 작업 불필요
- prod Aurora 의 DB 목록: `dpyb_auth`, `dpyb_book`, `dpyb_record`, `postgres`, `rdsadmin`
  — 공용 `dpyb` 가 없다. prod 는 처음부터 per-service 로 만들어져 있었다
  (`docs/db-per-service.md` 의 "prod 는 아직 적용하지 않았습니다" 는 낡은 서술이다)

### 남은 확인

- [x] 스키마 검증 완료 (CLIAR-183). `alembic upgrade head` 가 no-op 이었으므로
      `docs/db-per-service.md` 의 auth 고유 항목 4종을 직접 확인했다 — 전부 정상.

      | 확인 | 결과 |
      |---|---|
      | alembic 리비전 | `205eb1a0a7eb` — 저장소 체인 대조 결과 head 맞음 |
      | `uq_member_email_active` | `(email) WHERE (deleted_at IS NULL)` — predicate 유지 |
      | `uq_users_email` | 0행 (정상) |
      | `member_status` enum | ACTIVE / PENDING / WITHDRAWN |
      | `member_id_seq` | `last_value=1, is_called=false` — 행 0개이므로 정상 상태 |

      `member_id_seq` 의 `is_called=false` 는 db-per-service.md 가 경고한 상황이 아니다.
      그 경고는 행을 복사해 넣고 시퀀스를 올리지 않은 경우를 말하며, 여기는 member 행이
      0개라 충돌 대상이 없다(한 번도 쓰지 않은 시퀀스의 정상 상태).

      결론: `dpyb_auth` 는 alembic 이 정상적으로 전체 마이그레이션을 수행한 DB 다.
      이번 기동에서 no-op 이었던 것은 이미 완료돼 있었기 때문이다.
- [ ] DB 계정이 book 과 공용(`admin`)이다. dev 와 동일한 구성이라 일관성은 맞지만,
      database-per-service 취지상 `dpyb_auth` 전용 롤 분리는 후속 과제로 남는다
- [ ] 실패했던 구 ReplicaSet 4개가 남아 있다 (`5bb589c8b5`, `5cf8dcf`, `6899c5cddc`,
      `7cf6789c67` — 전부 desired 0). 정리 여부 판단 필요

절차와 확인 항목: `docs/db-per-service.md`

## B. IRSA — 보상/복구 경로 (Phase 0 잔여)

`app/core/cognito_auth.py`의 `admin_get_user`는 이미 구현돼 있으나 IAM 권한이 없어
동작하지 않는다. 정상 가입 경로는 영향받지 않지만, Cognito SignUp 성공 후 DB INSERT가
실패하면 고아 계정이 남는다.

CLIAR-183 에서 AWS 리소스와 매니페스트를 모두 만들었다. 남은 것은 배포 후 검증뿐이다.

- [x] EKS OIDC Identity Provider — dev 는 이미 등록돼 있었고, **prod 는 없어서 신규 생성**
      (`oidc.eks.ap-northeast-2.amazonaws.com/id/ABD868614E3238EEC532C73A888F74BE`)
- [x] IAM Policy 2개 — `dpyb-auth-cognito-admin-dev` / `-prod`
      (`cognito-idp:AdminGetUser`, `AdminDeleteUser` 2개 액션만, 각 환경 User Pool ARN 으로 스코핑)
- [x] IAM Role 2개 — `dpyb-auth-irsa-dev` / `dpyb-auth-irsa-prod`
      trust: `system:serviceaccount:dpyb-auth-dev:backend-auth` / `system:serviceaccount:dpyb-auth:backend-auth`
- [x] `k8s/base/serviceaccount.yaml` + `kustomization.yaml` 등록
- [x] `k8s/base/deployment.yaml` 에 `serviceAccountName: backend-auth`
- [x] overlay role-arn 패치 작성 및 활성화
      (`k8s/overlays/{dev,prod}/serviceaccount-patch.yaml`)
- [ ] 배포 후 검증 — 파드에 `AWS_ROLE_ARN` / `AWS_WEB_IDENTITY_TOKEN_FILE` 주입 확인,
      기존 Cognito 호출(SignUp/InitiateAuth)이 그대로 동작하는지 스모크

⚠ 배포 시 주의: role-arn 어노테이션이 붙으면 boto3 가 노드 인스턴스 롤 대신
`AssumeRoleWithWebIdentity` 를 쓴다. trust policy 의 네임스페이스/SA 이름이 틀리면
admin API 뿐 아니라 **모든 Cognito 호출이 자격증명 실패로 함께 깨진다.** dev 에서 먼저
확인한 뒤 prod 로 넘기는 것이 안전하다.

권한 범위와 폴백 정책: `docs/auth-architecture.md` §8.2

## C. FE 연동 문서 (Phase 8)

- [ ] `docs/auth-api.md` 작성 — 엔드포인트 계약, 쿠키 동작,
      모든 호출에 `credentials: "include"`가 필요하다는 점
      (초안 재료는 `docs/auth-architecture.md` §5와 §14)

## D. prod Cognito 구성

CLIAR-183 에서 prod User Pool 과 backend App Client 를 실제로 생성했다.
(계정 전체에 User Pool 이 dev 하나뿐이었다 — §2.4 의 placeholder 는 실제 미생성 상태였다)

| | User Pool | backend App Client |
|---|---|---|
| dev | `ap-northeast-2_y1mKz50El` | `du6sabh7hm17goodv6sd2n9ag` |
| prod | `ap-northeast-2_v5UmqpECS` (`dpyb-auth-prod`) | `3l3m4q175dh6l8u1iuksfp66d2` (`dpyb-auth-backend-prod`) |

prod 는 §8.1 사양대로 생성했고 User Pool 설정은 dev 와 동일하게 맞췄다
(email 로그인, email 자동 검증, 비밀번호 정책, CONFIRM_WITH_CODE, 삭제 보호 ACTIVE).
App Client 는 secret 있음 / `ALLOW_USER_PASSWORD_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH` 만 /
token revocation 활성 / prevent user existence errors 활성 / access·id 1일, refresh 30일.

- [x] prod User Pool / backend App Client 생성
- [x] prod `COGNITO_BACKEND_CLIENT_ID` ConfigMap 주입 (overlay `configmap-patch.yaml`)
- [x] prod `COGNITO_BACKEND_CLIENT_SECRET` 을 `backend-auth-secret` 에 주입 (CLIAR-183)
      — 기존에 이 키가 아예 없었다. 앱이 읽지 않는 `COGNITO_CLIENT_SECRET`(0바이트,
        Phase 7 에서 폐기된 키)만 남아 있었고, 그건 무해해서 건드리지 않았다
- [ ] prod `CORS_ALLOWED_ORIGINS` 주입 — **아직 넣을 값이 없다.** prod FE 오리진이
      존재하지 않는다(CloudFront 배포가 dev 하나뿐이고, prod ALB 는 도메인 없이
      HTTP:80 으로만 노출돼 있다). prod FE 배포 시점에 그 오리진을 명시해야 한다
- [ ] prod ALB 가 HTTP:80 이라 HTTPS 오리진이 생기기 전까지는 `COOKIE_SECURE=true`
      (base 기본값) 상태에서 브라우저가 refresh 쿠키를 저장하지 못한다. FE 연동 전 확인 필요

## 후속 티켓 후보 (범위 외)

- 소셜 로그인(Google/Kakao/Apple) — Hosted UI + Authorization Code 흐름
- MFA / `NEW_PASSWORD_REQUIRED` 등 Cognito 챌린지 처리
- Redis 또는 AWS WAF 기반 분산 rate limiting
  (현재 인메모리라 replicas 수만큼 실효 한도가 늘어난다)
- 관리자용 회원 관리 API
